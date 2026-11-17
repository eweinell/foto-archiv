"""Sammlungen: mehrere Quellordner, Ablage je Sammlung, Migration aus dem Format vor Sammlungen."""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

import pytest
import typer
from conftest import OLD, _write

from foto_archiv import migrate as mig
from foto_archiv import seal as seal_mod
from foto_archiv import volumes as vol_mod
from foto_archiv.classify import classify_media
from foto_archiv.cli import resolve_series
from foto_archiv.config import Collection, ConfigError, load_config, render_config
from foto_archiv.hddops import restore_hdd, verify_hdd
from foto_archiv.scan import scan
from foto_archiv.seal import SealError, seal_series
from foto_archiv.status import compute_status
from foto_archiv.volumes import find_connected, resolve_volume


@pytest.fixture
def multi(app, pictures, tmp_path):
    """Fotos + Videos (Renderings, eigener Ordner) + Musik (ohne Jahr) + Diashows innerhalb von Pictures."""
    videos = tmp_path / "Videos"
    _write(videos / "2024.Hochzeit" / "Film.mp4", 70_000, 1, OLD)
    _write(videos / "2024.Hochzeit" / "projekt.kdenlive", 2_000, 2, OLD)
    _write(videos / "Urlaub-ohne-Jahr" / "clip.mov", 10_000, 3, OLD)
    musik = tmp_path / "Musik"
    _write(musik / "Beatles" / "Abbey Road" / "01 Come Together.flac", 30_000, 4, OLD)
    _write(musik / "Beatles" / "cover.jpg", 3_000, 5, OLD)
    _write(musik / "2001 Kompilation" / "a.mp3", 5_000, 6, OLD)  # Jahreszahl im Namen, aber year = none
    # Diashows liegen (noch) unter Pictures, gehören aber zur Sammlung "diashows"
    _write(pictures / "Diashows" / "2025.Rueckblick" / "show.mp4", 40_000, 7, OLD)
    app.cfg.collections = [
        Collection(id="fotos", source_root=pictures),
        Collection(id="videos", source_root=videos, profile="media"),
        Collection(id="musik", source_root=musik, profile="media", year="none", archive_dir="Musik"),
        Collection(id="diashows", source_root=pictures / "Diashows", profile="media"),
    ]
    app.cfg.validate()
    return app


def _files(app):
    return {r["path"]: r for r in app.db.q("SELECT * FROM files")}


def test_scan_multiple_collections(multi):
    st = scan(multi)
    rows = _files(multi)
    assert rows["videos/2024.Hochzeit/Film.mp4"]["series"] == "videos/2024.Hochzeit"
    assert rows["videos/2024.Hochzeit/Film.mp4"]["cls"] == "video"
    assert rows["videos/2024.Hochzeit/projekt.kdenlive"]["cls"] == "other"
    assert rows["musik/Beatles/Abbey Road/01 Come Together.flac"]["cls"] == "audio"
    assert rows["musik/Beatles/cover.jpg"]["cls"] == "img-original"
    assert rows["diashows/2025.Rueckblick/show.mp4"]["series"] == "diashows/2025.Rueckblick"
    # verschachtelter Quellordner gehört nur zu "diashows", nicht zusätzlich zu "fotos"
    assert not any(p.startswith("fotos/Diashows/") for p in rows)
    assert rows["fotos/2026.Kenia/raws/DSC01.ARW"]["cls"] == "raw"
    assert set(st.collections) == {"fotos", "videos", "musik", "diashows"}
    years = {r["name"]: r["year"] for r in multi.db.q("SELECT * FROM series")}
    assert years["videos/2024.Hochzeit"] == 2024 and years["musik/2001 Kompilation"] is None


def test_missing_source_keeps_files(multi, tmp_path):
    scan(multi)
    shutil.move(tmp_path / "Videos", tmp_path / "Videos-abgezogen")  # externe Platte nicht angeschlossen
    st = scan(multi)
    assert st.missing == ["videos"] and st.gone == 0
    assert _files(multi)["videos/2024.Hochzeit/Film.mp4"]["present"] == 1


def test_seal_layout_per_collection(multi, hdd_a):
    scan(multi)
    seal_series(multi, "videos/2024.Hochzeit", hdd_a)
    seal_series(multi, "videos/Urlaub-ohne-Jahr", hdd_a)
    seal_series(multi, "musik/Beatles", hdd_a)
    seal_series(multi, "musik/2001 Kompilation", hdd_a)
    rel = {b["series"] + ":" + b["name"]: b["relpath"] for b in multi.db.q("SELECT * FROM bundles")}
    assert rel["videos/2024.Hochzeit:video-001.tar"] == "videos/2024/2024.Hochzeit/v1/video-001.tar"
    assert rel["videos/Urlaub-ohne-Jahr:video-001.tar"] == "videos/_ohne-jahr/Urlaub-ohne-Jahr/v1/video-001.tar"
    assert rel["musik/Beatles:audio-001.tar"] == "Musik/Beatles/v1/audio-001.tar"  # archive_dir, keine Jahresebene
    assert rel["musik/2001 Kompilation:audio-001.tar"] == "Musik/2001 Kompilation/v1/audio-001.tar"
    with tarfile.open(hdd_a.path_for(rel["musik/Beatles:audio-001.tar"])) as tf:
        assert tf.getnames() == ["Beatles/Abbey Road/01 Come Together.flac"]  # relativ zum Quellordner
    seal_mod.export_index(multi, hdd_a)
    readme = (hdd_a.root / "LIESMICH.txt").read_text(encoding="utf-8")
    assert "Musik/" in readme and "videos/" in readme


def test_restore_media_to_collection_root(multi, hdd_a, tmp_path):
    scan(multi)
    seal_series(multi, "musik/Beatles", hdd_a)
    flac = tmp_path / "Musik" / "Beatles" / "Abbey Road" / "01 Come Together.flac"
    data = flac.read_bytes()
    flac.unlink()
    res = restore_hdd(multi, "musik/Beatles", ["audio"], None, hdd_a, overwrite=False, dry_run=False)
    assert res.written == ["Beatles/Abbey Road/01 Come Together.flac"]
    assert flac.read_bytes() == data
    assert _files(multi)["musik/Beatles/Abbey Road/01 Come Together.flac"]["present"] == 1


def test_status_settle_days_per_collection(multi):
    scan(multi)
    multi.cfg.collections[1].settle_days = 100_000
    states = {s.name: s.state for s in compute_status(multi)}
    assert states["videos/2024.Hochzeit"] == "aktiv"  # eigene, längere Ruhezeit
    assert states["musik/Beatles"] == "abschlussreif"


def test_resolve_series(multi):
    scan(multi)
    assert resolve_series(multi, ["2026.Kenia"]) == ["fotos/2026.Kenia"]
    assert resolve_series(multi, ["musik/"]) == ["musik/2001 Kompilation", "musik/Beatles"]
    assert resolve_series(multi, ["musik"]) == ["musik/2001 Kompilation", "musik/Beatles"]
    assert resolve_series(multi, ["videos/2024.Hochzeit"]) == ["videos/2024.Hochzeit"]
    _write(multi.cfg.collections[1].source_root / "2026.Kenia" / "x.mp4", 100, 1)
    scan(multi)
    with pytest.raises(typer.Exit):  # gleicher Name in zwei Sammlungen -> mehrdeutig
        resolve_series(multi, ["2026.Kenia"])
    with pytest.raises(typer.Exit):
        resolve_series(multi, ["gibtsnicht/x"])


def test_classify_media():
    assert classify_media("song.FLAC") == "audio"
    assert classify_media("film.mkv") == "video"
    assert classify_media("foto.jpg") == "img-original"
    assert classify_media("projekt.kdenlive") == "other"


# --------------------------------------------------------------------------- große Dateien / FAT32


def test_fat32_refuses_oversized_bundle(app, hdd_a, monkeypatch):
    monkeypatch.setattr(vol_mod, "filesystem_type", lambda p: "FAT32")
    monkeypatch.setattr(seal_mod, "FAT_MAX_FILE", 20_000)  # statt 4 GiB: Datei > Grenze simulieren
    scan(app)
    with pytest.raises(SealError, match="FAT32"):
        seal_series(app, "fotos/2026.Kenia", hdd_a)  # Video 50 KB > Grenze
    assert not app.db.q("SELECT * FROM bundles")
    monkeypatch.setattr(vol_mod, "filesystem_type", lambda p: "exFAT")
    assert seal_series(app, "fotos/2026.Kenia", hdd_a) is not None


def test_single_file_larger_than_chunk(app, hdd_a, pictures):
    _write(pictures / "2024.Film" / "a.jpg", 1_000, 1, OLD)
    _write(pictures / "2024.Film" / "lang.mp4", 2_500_000, 2, OLD)  # > chunk_size_mb = 1
    _write(pictures / "2024.Film" / "kurz.mp4", 10_000, 3, OLD)
    scan(app)
    seal_series(app, "fotos/2024.Film", hdd_a)
    vids = [b for b in app.db.bundles_of("fotos/2024.Film") if b["cls"] == "video"]
    assert [b["nfiles"] for b in vids] == [1, 1]  # große Datei allein, Rest im nächsten Paket
    assert max(b["size"] for b in vids) > 1024 * 1024


# --------------------------------------------------------------------------- Konfiguration


def test_config_roundtrip_and_validation(tmp_path, multi):
    p = tmp_path / "config.toml"
    p.write_text(render_config(multi.cfg), encoding="utf-8")
    cfg = load_config(p)
    assert [c.id for c in cfg.collections] == ["fotos", "videos", "musik", "diashows"]
    assert cfg.collections[2].archive_dir == "Musik" and cfg.collections[2].year == "none"
    assert cfg.collections_explicit
    bad = render_config(multi.cfg).replace('archive_dir = "Musik"', 'archive_dir = "videos"')
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ConfigError, match="mehreren"):
        load_config(p)
    p.write_text(render_config(multi.cfg).replace('archive_dir = "Musik"', 'archive_dir = "2024"'), encoding="utf-8")
    with pytest.raises(ConfigError, match="Jahreszahl"):
        load_config(p)


def test_old_config_gets_implicit_collection(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'source_root = "{(tmp_path / "Bilder").as_posix()}"\n[s3]\nprefix = "fotos/"\n', encoding="utf-8")
    cfg = load_config(p)
    assert [(c.id, c.archive_dir) for c in cfg.collections] == [("fotos", "fotos")]
    assert cfg.collections[0].source_root == tmp_path / "Bilder" and not cfg.collections_explicit


# --------------------------------------------------------------------------- Migration


def _to_schema1(app, vol, old_prefix="fotos/"):
    """Stellt den Zustand vor den Sammlungen her (wie beim echten Bestand): Index, Volume, S3-ID."""
    db = app.db
    n = len("fotos/") + 1
    with db.tx() as c:
        c.execute("UPDATE files SET path=substr(path,?), series=substr(series,?)", (n, n))
        c.execute("UPDATE series SET name=substr(name,?)", (n,))
        c.execute("UPDATE versions SET series=substr(series,?), manifest_relpath=substr(manifest_relpath,?)", (n, n))
        c.execute("UPDATE bundles SET series=substr(series,?), relpath=substr(relpath,?)", (n, n))
        c.execute("UPDATE archived SET path=substr(path,?), series=substr(series,?)", (n, n))
        c.execute("UPDATE manifest_copies SET series=substr(series,?)", (n,))
        old_id = f"s3:{app.cfg.s3.bucket}/{old_prefix}"
        c.execute("INSERT INTO volumes(id, kind, label, created) VALUES(?, 's3', 'S3', '2026-01-01')", (old_id,))
        for b in c.execute("SELECT id FROM bundles").fetchall():
            c.execute("INSERT INTO copies(bundle_id, volume_id, stored_at, verified_at, status) VALUES(?,?,?,?,'ok')",
                      (b[0], old_id, "2026-01-01", "2026-01-01"))
        c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    for top in (vol.root / "fotos").iterdir():
        shutil.move(top, vol.root / top.name)
    (vol.root / "fotos").rmdir()
    marker = vol.root / "volume.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    del data["layout"]
    marker.write_text(json.dumps(data), encoding="utf-8")
    app.cfg.s3.prefix = old_prefix
    app.cfg.collections_explicit = False
    return old_id


def test_migration_keeps_s3_keys_and_moves_hdd(app, hdd_a, tmp_path):
    scan(app)
    seal_series(app, "fotos/2026.Kenia", hdd_a)
    seal_series(app, "fotos/sortiert", hdd_a)
    old_id = _to_schema1(app, hdd_a)
    old_keys = sorted("fotos/" + r["relpath"] for r in app.db.q("SELECT relpath FROM bundles"))
    assert all(k.startswith(("fotos/2026/", "fotos/_ohne-jahr/")) for k in old_keys)
    assert (hdd_a.root / "2026" / "2026.Kenia" / "v1").is_dir()

    # Volume im alten Format wird nicht benutzt
    assert all(v.id != hdd_a.id for v in find_connected(app.cfg, app.db))
    with pytest.raises(vol_mod.VolumeError, match="hdd migrate"):
        resolve_volume(app.cfg, app.db, "A")

    app.cfg.path = tmp_path / "cfg" / "config.toml"
    app.cfg.path.parent.mkdir()
    app.cfg.path.write_text("# alt\n", encoding="utf-8")
    plan = mig.plan_index(app)
    assert plan.new_prefix == "" and plan.collection.archive_dir == "fotos"
    mig.migrate_index(app, plan)

    assert app.db.schema_version == 2 and app.cfg.s3.prefix == ""
    new_keys = sorted(app.cfg.s3.prefix + r["relpath"] for r in app.db.q("SELECT relpath FROM bundles"))
    assert new_keys == old_keys  # S3 unverändert
    new_id = app.cfg.s3.volume_id
    assert new_id == "s3:test-bucket/" and not app.db.q1("SELECT 1 FROM volumes WHERE id=?", (old_id,))
    assert app.db.q1("SELECT COUNT(*) n FROM copies WHERE volume_id=?", (new_id,))["n"] == len(old_keys)
    assert app.db.q1("SELECT 1 FROM files WHERE path='fotos/2026.Kenia/raws/DSC01.ARW'")
    written = load_config(app.cfg.path)
    assert [(c.id, c.archive_dir) for c in written.collections] == [("fotos", "fotos")] and written.s3.prefix == ""
    assert list(app.cfg.path.parent.glob("config.toml.schema1-*.bak"))
    assert list(app.cfg.index_path.parent.glob("index-schema1-*.sqlite"))

    (old_vol,) = [v for v in find_connected(app.cfg, app.db, include_outdated=True) if v.id == hdd_a.id]
    assert old_vol.outdated
    new_vol, res = mig.migrate_volume(app, old_vol)
    assert not new_vol.outdated and not res.missing and sorted(res.moved) == ["2026 -> fotos/2026", "_ohne-jahr -> fotos/_ohne-jahr"]
    assert (hdd_a.root / "fotos" / "2026" / "2026.Kenia" / "v1").is_dir()
    assert not verify_hdd(app, new_vol, None, quick=False).failed

    # danach normal weiterarbeiten: v2 landet im neuen Format, Restore findet alte Pakete
    (app.cfg.source_root / "2026.Kenia" / "jpg" / "neu.jpg").write_bytes(b"neu")
    scan(app)
    plan2 = seal_series(app, "fotos/2026.Kenia", new_vol)
    assert plan2.version == 2
    (app.cfg.source_root / "2026.Kenia" / "raws" / "DSC01.ARW").unlink()
    res = restore_hdd(app, "fotos/2026.Kenia", ["raw"], None, new_vol, overwrite=False, dry_run=False)
    assert "2026.Kenia/raws/DSC01.ARW" in res.written


def test_migration_refuses_when_keys_would_change(app, hdd_a):
    scan(app)
    seal_series(app, "fotos/2026.Kenia", hdd_a)
    _to_schema1(app, hdd_a)
    app.cfg.collections_explicit = True  # z. B. Konfiguration schon von Hand umgestellt ...
    app.cfg.collections[0].archive_dir = "bilder"  # ... aber mit anderem Ordnernamen
    app.cfg.s3.prefix = ""
    with pytest.raises(mig.MigrationError, match="S3-Keys würden sich ändern"):
        mig.plan_index(app)


def test_migrate_volume_requires_index(app, hdd_a):
    scan(app)
    seal_series(app, "fotos/2026.Kenia", hdd_a)
    _to_schema1(app, hdd_a)
    (v,) = [v for v in find_connected(app.cfg, app.db, include_outdated=True) if v.id == hdd_a.id]
    _, res = mig.migrate_volume(app, v, dry_run=True)
    assert res.moved == ["2026 -> fotos/2026"]
    with pytest.raises(mig.MigrationError):
        mig.migrate_volume(app, v)
    assert (hdd_a.root / "2026").is_dir()  # nichts bewegt


def test_new_volume_has_current_layout(app, tmp_path):
    v = vol_mod.init_volume(tmp_path / "hddC", "C", app.db, "medien-archiv")
    assert v.root == tmp_path / "hddC" / "medien-archiv" and not v.outdated
    app.cfg.hdd_paths.append(str(tmp_path / "hddC"))
    app.cfg.volume_dir = "medien-archiv"
    assert any(x.id == v.id for x in find_connected(app.cfg, app.db))
    # Archivordner auf der Platte umbenannt: Erkennung über volume.json, nicht über den Namen
    (tmp_path / "hddC" / "medien-archiv").rename(tmp_path / "hddC" / "Archiv")
    app.cfg.volume_dir = "foto-archiv"
    (found,) = [x for x in find_connected(app.cfg, app.db) if x.id == v.id]
    assert found.root == tmp_path / "hddC" / "Archiv"


def test_seal_unknown_collection(app):
    with pytest.raises(SealError, match="nicht konfiguriert"):
        seal_mod.plan_seal(app, "gibtsnicht/x")


def test_conftest_paths_are_tmp(app, tmp_path):
    assert Path(app.cfg.collections[0].source_root).is_relative_to(tmp_path)
