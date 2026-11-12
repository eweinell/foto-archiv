"""Tests der S3-Schicht gegen moto (keine echten AWS-Aufrufe)."""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3

import boto3
import pytest
import typer
from moto import mock_aws

from foto_archiv import s3client, s3ops
from foto_archiv.s3ops import catalog_backup, legacy_scan, push_s3, restore_s3, verify_s3
from foto_archiv.scan import scan
from foto_archiv.seal import seal_series

BUCKET = "test-bucket"
PREFIX = "fotos/"


@pytest.fixture
def s3(monkeypatch, tmp_path):
    """moto-Bucket in eu-west-1 (versioniert, wie der echte) + Fake-Zugangsdaten."""
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "eu-west-1",
        "AWS_CONFIG_FILE": str(tmp_path / "keine-aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "keine-aws-credentials"),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(s3ops, "RETRY_DELAY", 0)
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        c.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        c.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield c


@pytest.fixture
def hooks(monkeypatch):
    """Registriert botocore-Event-Hooks an jedem vom Tool erzeugten Client."""
    registered: list[tuple[str, object]] = []
    orig = s3client.make_client

    def make(cfg):
        client = orig(cfg)
        for event, fn in registered:
            client.meta.events.register(event, fn)
        return client

    monkeypatch.setattr(s3client, "make_client", make)
    return registered


def _key(relpath: str) -> str:
    return PREFIX + relpath


def _keys(c, prefix: str = PREFIX) -> list[str]:
    return [o["Key"] for o in c.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])]


def _sealed(app, hdd, *series: str) -> None:
    scan(app)
    for s in series:
        seal_series(app, s, hdd)


def _big_series(app, hdd, size: int = 12 * 1024 * 1024):
    """Serie mit einem Paket > 10 MiB (bei part_size_mb=5 -> 3 Teile)."""
    p = app.cfg.source_root / "2025.Gross" / "BIG.ARW"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    _sealed(app, hdd, "2025.Gross")
    app.cfg.s3.part_size_mb = 5
    (b,) = app.db.bundles_of("2025.Gross")
    assert b["size"] > 10 * 1024 * 1024
    return b


# ---------------------------------------------------------------------------- push


def test_push_uploads_bundles_manifest_and_index(app, hdd_a, s3, capsys):
    _sealed(app, hdd_a, "2026.Kenia", "2013.Österreich")
    push_s3(app, None, None, dry_run=True)
    assert "Würde hochladen" in capsys.readouterr().out
    assert _keys(s3) == []

    push_s3(app, None, None, dry_run=False)
    vol = app.cfg.s3.volume_id
    bundles = app.db.q("SELECT * FROM bundles")
    assert len(bundles) >= 5
    for b in bundles:
        h = s3.head_object(Bucket=BUCKET, Key=_key(b["relpath"]))
        assert h["StorageClass"] == "DEEP_ARCHIVE"
        assert h["ContentLength"] == b["size"]
        assert h["Metadata"]["sha256"] == b["sha256"]
        assert h["Metadata"]["tool"] == "foto-archiv"
        c = app.db.q1("SELECT * FROM copies WHERE bundle_id=? AND volume_id=?", (b["id"], vol))
        assert c["status"] == "ok" and c["verified_at"]
    # Umlaut-Serie: Key mit Umlaut, Metadaten ASCII (prozentkodiert)
    oe = next(b for b in bundles if b["series"] == "2013.Österreich")
    h = s3.head_object(Bucket=BUCKET, Key=_key(oe["relpath"]))
    assert s3client.meta_decode(h["Metadata"]["series"]) == "2013.Österreich"

    for v in app.db.q("SELECT * FROM versions"):
        h = s3.head_object(Bucket=BUCKET, Key=_key(v["manifest_relpath"]))
        assert h.get("StorageClass", "STANDARD") == "STANDARD"
        assert app.db.q1(
            "SELECT 1 FROM manifest_copies WHERE series=? AND version=? AND volume_id=?", (v["series"], v["version"], vol)
        )
    assert app.db.q1("SELECT kind FROM volumes WHERE id=?", (vol,))["kind"] == "s3"
    assert app.db.q("SELECT * FROM uploads") == []

    snaps = _keys(s3, PREFIX + "_index/")
    assert len(snaps) == 1 and snaps[0].endswith(".sqlite.gz")
    raw = gzip.decompress(s3.get_object(Bucket=BUCKET, Key=snaps[0])["Body"].read())
    snap = app.cfg.index_path.parent / "snap.sqlite"
    snap.write_bytes(raw)
    con = sqlite3.connect(snap)
    assert con.execute("SELECT COUNT(*) FROM bundles").fetchone()[0] == len(bundles)
    con.close()

    # zweiter Lauf: nichts zu tun, keine neue Index-Sicherung
    n_objects = len(_keys(s3))
    push_s3(app, None, None, dry_run=False)
    assert "Alles bereits in S3" in capsys.readouterr().out
    assert len(_keys(s3)) == n_objects


def test_push_registers_existing_identical_object(app, hdd_a, s3):
    """Objekt liegt schon (identisch) in S3, Index weiß nichts -> nur nachtragen, nicht neu schreiben."""
    _sealed(app, hdd_a, "2013.Österreich")
    push_s3(app, None, None, dry_run=False)
    app.db.x("DELETE FROM copies WHERE volume_id=?", (app.cfg.s3.volume_id,))
    versions_before = s3.list_object_versions(Bucket=BUCKET, Prefix=PREFIX + "2013/")["Versions"]
    push_s3(app, None, None, dry_run=False)
    versions_after = s3.list_object_versions(Bucket=BUCKET, Prefix=PREFIX + "2013/")["Versions"]
    assert len(versions_after) == len(versions_before)  # nichts überschrieben
    assert app.db.q1("SELECT COUNT(*) n FROM copies WHERE volume_id=? AND status='ok'", (app.cfg.s3.volume_id,))["n"] == 1


def test_push_conflict_never_overwrites(app, hdd_a, s3):
    _sealed(app, hdd_a, "2013.Österreich")
    (b,) = app.db.bundles_of("2013.Österreich")
    s3.put_object(Bucket=BUCKET, Key=_key(b["relpath"]), Body=b"fremder Inhalt", Metadata={"sha256": "0" * 64})
    with pytest.raises(typer.Exit) as ei:
        push_s3(app, ["2013.Österreich"], hdd_a, dry_run=False)
    assert ei.value.exit_code == 1
    assert s3.get_object(Bucket=BUCKET, Key=_key(b["relpath"]))["Body"].read() == b"fremder Inhalt"
    assert not app.db.q1("SELECT 1 FROM copies WHERE volume_id=?", (app.cfg.s3.volume_id,))


def test_push_skips_without_connected_hdd(app, hdd_a, s3, capsys):
    _sealed(app, hdd_a, "2013.Österreich")
    shutil.rmtree(hdd_a.root)  # HDD "abgezogen"
    push_s3(app, None, None, dry_run=False)
    assert "keine HDD" in capsys.readouterr().out
    assert _keys(s3) == []


def test_push_multipart(app, hdd_a, s3, hooks):
    parts: list[int] = []
    hooks.append(("before-parameter-build.s3.UploadPart", lambda params, **kw: parts.append(params["PartNumber"])))
    b = _big_series(app, hdd_a)
    push_s3(app, None, None, dry_run=False)
    assert sorted(parts) == [1, 2, 3]
    key = _key(b["relpath"])
    h = s3.head_object(Bucket=BUCKET, Key=key)
    assert h["StorageClass"] == "DEEP_ARCHIVE" and h["ContentLength"] == b["size"]
    assert h["Metadata"]["sha256"] == b["sha256"]
    assert h["Metadata"]["part-size"] == str(5 * 1024 * 1024)
    # Inhalt stimmt (moto stellt Deep-Archive-Objekte sofort wieder her)
    s3.restore_object(Bucket=BUCKET, Key=key, RestoreRequest={"Days": 1, "GlacierJobParameters": {"Tier": "Bulk"}})
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert hashlib.sha256(body).hexdigest() == b["sha256"]
    assert s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []


def test_push_resume_after_abort(app, hdd_a, s3, hooks):
    app.cfg.s3.concurrency = 1
    state = {"abort": True, "parts": [], "creates": 0}

    def on_part(params, **kw):
        if state["abort"] and params["PartNumber"] == 2:
            raise KeyboardInterrupt  # simuliert Strg+C / Prozessende mitten im Upload
        state["parts"].append(params["PartNumber"])

    def on_create(params, **kw):
        state["creates"] += 1

    hooks.append(("before-parameter-build.s3.UploadPart", on_part))
    hooks.append(("before-parameter-build.s3.CreateMultipartUpload", on_create))
    b = _big_series(app, hdd_a)
    key = _key(b["relpath"])

    with pytest.raises(typer.Exit) as ei:
        push_s3(app, None, None, dry_run=False)
    assert ei.value.exit_code == 130
    row = app.db.q1("SELECT * FROM uploads WHERE key=?", (key,))
    assert row is not None and row["bundle_id"] == b["id"]
    assert [p["PartNumber"] for p in s3.list_parts(Bucket=BUCKET, Key=key, UploadId=row["upload_id"])["Parts"]] == [1]
    assert state["parts"] == [1] and state["creates"] == 1
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix=key)

    state["abort"] = False
    push_s3(app, None, None, dry_run=False)
    assert state["creates"] == 1  # gleiche UploadId weiterverwendet
    assert state["parts"] == [1, 2, 3]  # Teil 1 nicht erneut übertragen
    assert app.db.q("SELECT * FROM uploads") == []
    h = s3.head_object(Bucket=BUCKET, Key=key)
    assert h["ContentLength"] == b["size"] and h["Metadata"]["sha256"] == b["sha256"]
    assert app.db.q1("SELECT status FROM copies WHERE bundle_id=? AND volume_id=?", (b["id"], app.cfg.s3.volume_id))["status"] == "ok"


def test_push_resume_with_vanished_upload(app, hdd_a, s3):
    """UploadId im Index, in S3 aber nicht mehr vorhanden -> neu beginnen."""
    b = _big_series(app, hdd_a)
    key = _key(b["relpath"])
    app.db.x(
        "INSERT INTO uploads(key, upload_id, bundle_id, part_size, size, sha256, started_at) VALUES(?,?,?,?,?,?,?)",
        (key, "gibt-es-nicht", b["id"], 5 * 1024 * 1024, b["size"], b["sha256"], "2026-01-01T00:00:00+00:00"),
    )
    push_s3(app, None, None, dry_run=False)
    assert s3.head_object(Bucket=BUCKET, Key=key)["ContentLength"] == b["size"]
    assert app.db.q("SELECT * FROM uploads") == []


def test_put_small_conditional(app, s3):
    """If-None-Match: * greift serverseitig (Wettlauf zwischen HEAD und PUT)."""
    conn = s3ops._connect(app)
    data = b"manifest"
    sha = hashlib.sha256(data).hexdigest()
    assert s3ops.put_small(conn, "fotos/k", data, "STANDARD", {"sha256": sha}, sha) == "uploaded"
    assert s3ops.put_small(conn, "fotos/k", data, "STANDARD", {"sha256": sha}, sha) == "exists"
    other = b"anders!!"
    with pytest.raises(s3ops.UploadConflict):
        s3ops.put_small(conn, "fotos/k", other, "STANDARD", {"sha256": hashlib.sha256(other).hexdigest()}, hashlib.sha256(other).hexdigest())
    assert len(s3.list_object_versions(Bucket=BUCKET, Prefix="fotos/k")["Versions"]) == 1


@pytest.mark.parametrize("identical", [True, False])
def test_complete_multipart_precondition(app, hdd_a, s3, hooks, identical):
    """Objekt taucht zwischen HEAD und CompleteMultipartUpload auf -> nie überschreiben."""
    b = _big_series(app, hdd_a)
    key = _key(b["relpath"])
    body = hdd_a.path_for(b["relpath"]).read_bytes() if identical else b"fremd"

    def sneak_in(params, **kw):
        s3.put_object(Bucket=BUCKET, Key=key, Body=body, Metadata={"sha256": b["sha256"]})

    hooks.append(("before-parameter-build.s3.CompleteMultipartUpload", sneak_in))
    if identical:
        push_s3(app, None, None, dry_run=False)
        assert app.db.q1("SELECT status FROM copies WHERE bundle_id=? AND volume_id=?", (b["id"], app.cfg.s3.volume_id))["status"] == "ok"
    else:
        with pytest.raises(typer.Exit):
            push_s3(app, None, None, dry_run=False)
        assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"fremd"
    assert len(s3.list_object_versions(Bucket=BUCKET, Prefix=key)["Versions"]) == 1
    assert s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []  # abgebrochen, keine Teile-Leichen
    assert app.db.q("SELECT * FROM uploads") == []


def test_part_size():
    MiB = 1024 * 1024
    assert s3ops._part_size(64, 100) == 64 * MiB
    assert s3ops._part_size(1, 100) == 5 * MiB  # S3-Minimum
    size = 50 * 1024**3
    assert s3ops._part_size(64, size) == 64 * MiB
    huge = 2 * 1024**4
    ps = s3ops._part_size(64, huge)
    assert -(-huge // ps) <= 9000 and ps % MiB == 0


# ---------------------------------------------------------------------------- verify


def test_verify_s3(app, hdd_a, s3, capsys):
    _sealed(app, hdd_a, "2026.Kenia", "2013.Österreich")
    push_s3(app, ["2026.Kenia"], None, dry_run=False)
    vol = app.cfg.s3.volume_id
    verify_s3(app, None, deep=True)
    out = capsys.readouterr().out
    assert "ohne Befund" in out
    kenia = app.db.bundles_of("2026.Kenia")
    for b in kenia:
        assert app.db.q1("SELECT status FROM copies WHERE bundle_id=? AND volume_id=?", (b["id"], vol))["status"] == "ok"

    # 1) Kenia-Paket verschwindet (nur im Test möglich) -> missing
    gone = kenia[0]
    s3.delete_object(Bucket=BUCKET, Key=_key(gone["relpath"]))
    # 2) unbekanntes, falsches Objekt unter dem Key eines nicht hochgeladenen Pakets -> abweichend
    (oe,) = app.db.bundles_of("2013.Österreich")
    s3.put_object(Bucket=BUCKET, Key=_key(oe["relpath"]), Body=b"x" * 10, Metadata={"sha256": oe["sha256"]})
    with pytest.raises(typer.Exit):
        verify_s3(app, None, deep=False)
    out = capsys.readouterr().out
    assert "fehlt in S3" in out and "Größe 10" in out
    assert app.db.q1("SELECT status FROM copies WHERE bundle_id=? AND volume_id=?", (gone["id"], vol))["status"] == "missing"
    assert not app.db.q1("SELECT 1 FROM copies WHERE bundle_id=? AND volume_id=?", (oe["id"], vol))

    # 3) Kenia-Paket mit falscher Größe trotz Index-Eintrag -> bad
    other = kenia[1]
    s3.put_object(Bucket=BUCKET, Key=_key(other["relpath"]), Body=b"kaputt", Metadata={"sha256": other["sha256"]})
    with pytest.raises(typer.Exit):
        verify_s3(app, ["2026.Kenia"], deep=False)
    assert app.db.q1("SELECT status FROM copies WHERE bundle_id=? AND volume_id=?", (other["id"], vol))["status"] == "bad"


def test_verify_detects_unregistered_identical_object(app, hdd_a, s3, capsys):
    _sealed(app, hdd_a, "2013.Österreich")
    push_s3(app, None, None, dry_run=False)
    app.db.x("DELETE FROM copies WHERE volume_id=?", (app.cfg.s3.volume_id,))
    verify_s3(app, None, deep=False)
    assert "nachgetragen" in capsys.readouterr().out
    (b,) = app.db.bundles_of("2013.Österreich")
    assert app.db.q1("SELECT status FROM copies WHERE bundle_id=? AND volume_id=?", (b["id"], app.cfg.s3.volume_id))["status"] == "ok"


# ---------------------------------------------------------------------------- restore


def test_restore_from_s3(app, hdd_a, s3, hooks, capsys):
    gets: list[str] = []
    hooks.append(("before-parameter-build.s3.GetObject", lambda params, **kw: gets.append(params["Key"])))
    root = app.cfg.source_root
    kenia = root / "2026.Kenia"
    _sealed(app, hdd_a, "2026.Kenia")
    # v2: geänderter Export -> beim Wiederherstellen muss die neueste Fassung gewinnen
    (kenia / "jpg" / "DSC02.jpg").write_bytes(b"neu bearbeitet v2")
    scan(app)
    seal_series(app, "2026.Kenia", hdd_a)
    push_s3(app, None, None, dry_run=False)

    originals = {p.relative_to(root).as_posix(): p.read_bytes() for p in kenia.rglob("*") if p.is_file()}
    shutil.rmtree(kenia / "raws")
    shutil.rmtree(kenia / "jpg")
    scan(app)
    shutil.rmtree(hdd_a.root)  # nur S3 verfügbar

    restore_s3(app, "2026.Kenia", ["raw", "img-derived"], root, None, overwrite=False, dry_run=True)
    assert "Kostenschätzung" in capsys.readouterr().out
    assert app.db.q("SELECT * FROM restores") == []

    # Lauf 1: anfordern
    restore_s3(app, "2026.Kenia", ["raw", "img-derived"], root, None, overwrite=False, dry_run=False)
    out = capsys.readouterr().out
    assert "angefordert" in out
    reqs = app.db.q("SELECT * FROM restores")
    assert reqs and all(r["status"] == "requested" and r["tier"] == "Bulk" for r in reqs)
    assert not (kenia / "raws").exists()

    # Lauf 2: (moto stellt sofort bereit) herunterladen, prüfen, entpacken
    restore_s3(app, "2026.Kenia", ["raw", "img-derived"], root, None, overwrite=False, dry_run=False)
    for rel, data in originals.items():
        if "/raws/" in rel or "/jpg/" in rel:
            assert (root / rel).read_bytes() == data, rel
    assert (kenia / "jpg" / "DSC02.jpg").read_bytes() == b"neu bearbeitet v2"
    assert all(r["status"] == "downloaded" for r in app.db.q("SELECT * FROM restores"))
    assert not (root.parent / f".{root.name}{s3ops.STAGING_SUFFIX}").exists()
    for r in app.db.q("SELECT * FROM files WHERE series='2026.Kenia' AND cls IN ('raw','img-derived')"):
        st = (root / r["path"]).stat()
        assert r["present"] == 1 and r["slimmed"] == 0
        assert r["size"] == st.st_size and r["mtime_ns"] == st.st_mtime_ns
        assert r["sha256"] == hashlib.sha256((root / r["path"]).read_bytes()).hexdigest()
    st = scan(app)
    assert st.changed == 0 and st.new == 0

    # Lauf 3: alles vorhanden -> kein Download mehr
    gets.clear()
    restore_s3(app, "2026.Kenia", ["raw", "img-derived"], root, None, overwrite=False, dry_run=False)
    assert gets == []
    assert "bereits vorhanden" in capsys.readouterr().out


def test_restore_into_other_target(app, hdd_a, s3, tmp_path):
    _sealed(app, hdd_a, "2013.Österreich")
    push_s3(app, None, None, dry_run=False)
    target = tmp_path / "ziel"
    target.mkdir()
    restore_s3(app, "2013.Österreich", None, target, "standard", overwrite=False, dry_run=False)
    restore_s3(app, "2013.Österreich", None, target, "standard", overwrite=False, dry_run=False)
    src = app.cfg.source_root / "2013.Österreich"
    for p in src.iterdir():
        assert (target / "2013.Österreich" / p.name).read_bytes() == p.read_bytes()
    assert {r["tier"] for r in app.db.q("SELECT tier FROM restores")} == {"Standard"}


# ---------------------------------------------------------------------------- Altbestand


def test_legacy_scan(app, hdd_a, s3, capsys):
    _sealed(app, hdd_a, "2013.Österreich")
    push_s3(app, None, None, dry_run=False)
    s3.put_object(Bucket=BUCKET, Key="fotos/2007/", Body=b"")
    s3.put_object(Bucket=BUCKET, Key="fotos/2007/2007.Schottland.tar", Body=b"t" * 1000, StorageClass="DEEP_ARCHIVE")
    s3.put_object(Bucket=BUCKET, Key="fotos/2013/2013_folders_20250928_102513.tar", Body=b"t" * 500, StorageClass="DEEP_ARCHIVE")
    s3.put_object(Bucket=BUCKET, Key="fotos/ew2.jpg", Body=b"j" * 10)
    s3.put_object(Bucket=BUCKET, Key="anderswo/x.tar", Body=b"x")
    legacy_scan(app)
    out = capsys.readouterr().out
    rows = {r["key"]: r for r in app.db.q("SELECT * FROM legacy")}
    assert set(rows) == {
        "fotos/2007/2007.Schottland.tar",
        "fotos/2013/2013_folders_20250928_102513.tar",
        "fotos/ew2.jpg",
    }
    assert rows["fotos/2007/2007.Schottland.tar"]["storage_class"] == "DEEP_ARCHIVE"
    assert rows["fotos/ew2.jpg"]["storage_class"] == "STANDARD"
    assert "2013.Österreich" in out or "Jahr 2013" in out
    assert "Altbestand: 3 Objekt(e)" in out
    # erneuter Lauf: idempotent
    legacy_scan(app)
    assert app.db.q1("SELECT COUNT(*) n FROM legacy")["n"] == 3


def test_tool_key_classification():
    assert s3ops.is_tool_key("2026/2026.Kenia/v1/raw-001.tar")
    assert s3ops.is_tool_key("_ohne-jahr/sortiert/v12/manifest.json.gz")
    assert s3ops.is_tool_key("_index/index-20261112T000000Z.sqlite.gz")
    assert s3ops.is_tool_key("_kataloge/20261112T000000Z/digikam4.db.gz")
    assert not s3ops.is_tool_key("2007/2007.Schottland.tar")
    assert not s3ops.is_tool_key("2024/2024_folders_20250928_102513.tar")
    assert not s3ops.is_tool_key("ew2.jpg")


# ---------------------------------------------------------------------------- Kataloge


def test_catalog_backup(app, hdd_a, s3, capsys):
    root = app.cfg.source_root
    (root / "digikam4.db").unlink()
    con = sqlite3.connect(root / "digikam4.db")
    con.execute("CREATE TABLE Albums(id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO Albums(name) VALUES(?)", [("Kenia",), ("Österreich",)])
    con.commit()  # Verbindung bleibt offen - wie bei laufendem digiKam
    (root / "recognition.db").write_bytes(b"kein sqlite")
    try:
        catalog_backup(app, hdd_a, to_s3=True)
    finally:
        con.close()
    out = capsys.readouterr().out
    assert "recognition.db: keine SQLite-Datenbank" in out

    rows = app.db.q("SELECT * FROM catalogs")
    assert {r["volume_id"] for r in rows} == {hdd_a.id, app.cfg.s3.volume_id}
    hdd_row = next(r for r in rows if r["volume_id"] == hdd_a.id)
    gz_hdd = hdd_a.path_for(hdd_row["location"]).read_bytes()
    assert hashlib.sha256(gz_hdd).hexdigest() == hdd_row["sha256"]
    restored = app.cfg.index_path.parent / "kat.sqlite"
    restored.write_bytes(gzip.decompress(gz_hdd))
    c2 = sqlite3.connect(restored)
    assert [r[0] for r in c2.execute("SELECT name FROM Albums ORDER BY id")] == ["Kenia", "Österreich"]
    c2.close()

    (key,) = _keys(s3, PREFIX + "_kataloge/")
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    assert obj["Body"].read() == gz_hdd
    assert s3.head_object(Bucket=BUCKET, Key=key).get("StorageClass", "STANDARD") == "STANDARD"


def test_missing_bucket_config(app, capsys):
    app.cfg.s3.bucket = ""
    with pytest.raises(typer.Exit):
        push_s3(app, None, None, dry_run=True)
    assert "Kein S3-Bucket" in capsys.readouterr().out
