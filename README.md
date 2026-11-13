# foto-archiv

Archiviert Fotoserien klassenweise (RAW, Exporte, Original-Bilder, Video) als Tar-Pakete auf
Archiv-HDDs und nach S3 Glacier Deep Archive. Hintergrund und Kosten: [STRATEGIE.md](STRATEGIE.md).

## Installation

```powershell
uv sync                       # Entwicklungsumgebung
uv run foto-archiv --help
# oder als Werkzeug systemweit:
uv tool install --editable .
foto-archiv --help
```

## AWS-Zugang

Einmalig mit einem Admin-Profil den eingeschränkten Tool-Benutzer anlegen (kein Löschrecht,
Bucket-Versionierung schützt vor Überschreiben):

```powershell
.\scripts\New-FotoArchivIamUser.ps1 -BucketName mein-foto-bucket -AdminProfile admin -EnableVersioning
```

Der Zugangsschlüssel liegt danach im AWS-CLI-Profil `foto-archiv` (`%USERPROFILE%\.aws\credentials`),
das Tool nutzt dieses Profil (`[s3] profile` in der Konfiguration).

- **Temporär nutzen / aufräumen:** `.\scripts\New-FotoArchivIamUser.ps1 -Remove -AdminProfile admin`
  löscht den Schlüssel in IAM und das Profil lokal (Benutzer + Policy bleiben, `-DeleteUser` entfernt auch diese).
- **Schlüssel erneuern:** `... -BucketName mein-foto-bucket -RotateKey`
- Alternativ nur für eine Shell-Sitzung: `$env:AWS_ACCESS_KEY_ID`, `$env:AWS_SECRET_ACCESS_KEY` setzen und
  in der Konfiguration `profile = ""`.

## Ablauf

```powershell
foto-archiv init --bucket mein-foto-bucket --prefix fotos/   # Konfiguration anlegen
foto-archiv scan                     # Bestand erfassen (nur lesend)
foto-archiv status                   # Übersicht je Serie
foto-archiv status 2026.Kenia        # Details

foto-archiv hdd init E:\ --label A   # Platte einmalig als Archiv-Volume einrichten
foto-archiv seal 2026.Kenia          # Version v1 auf die angeschlossene HDD packen
foto-archiv seal --ripe              # alle abschlussreifen/geänderten Serien
foto-archiv push-s3                  # fehlende Pakete von der HDD nach Deep Archive
foto-archiv push-s3 --limit 2MB/s    # mit Bandbreitenbegrenzung (auch 16Mbit, 500k; 0 = unbegrenzt)
foto-archiv copy-hdd --from A --to B # zweite Platte nachziehen
foto-archiv verify hdd -v B          # Platte prüfen (jährlich)
foto-archiv verify s3

foto-archiv slim                     # Vorschau: welche RAWs lokal entfernt werden können
foto-archiv slim --apply

foto-archiv restore 2026.Kenia --class raw            # von HDD (falls angeschlossen) sonst S3
foto-archiv restore 2026.Kenia --from s3 --tier Bulk  # mehrfach aufrufen bis fertig (≤ 48 h)

foto-archiv legacy scan              # Altbestand im Bucket auflisten
foto-archiv catalog backup           # digiKam-Datenbanken sichern
```

Konfiguration: `%LOCALAPPDATA%\foto-archiv\config.toml`, Index: `%LOCALAPPDATA%\foto-archiv\index.sqlite`
(eine Kopie des Index liegt auf jedem Volume unter `foto-archiv\_index\` und als Schnappschuss in S3).

Dauerhafte Upload-Bandbreite: in der Konfiguration unter `[s3]` z. B. `upload_limit = "2MB/s"` eintragen;
`push-s3 --limit` überschreibt den Wert für einen Lauf. Die Grenze gilt gemeinsam für alle parallelen
Teil-Uploads und wird gleichmäßig im Datenstrom eingehalten (keine Lastspitzen).

## Wiederherstellung ohne Tool

```powershell
aws s3api restore-object --bucket mein-foto-bucket --key "fotos/2026/2026.Kenia/v1/raw-001.tar" `
    --restore-request '{\"Days\":7,\"GlacierJobParameters\":{\"Tier\":\"Bulk\"}}' --profile foto-archiv
# nach bis zu 48 h:
aws s3 cp "s3://mein-foto-bucket/fotos/2026/2026.Kenia/v1/raw-001.tar" . --profile foto-archiv
tar -xf raw-001.tar -C $HOME\Pictures
```

Welche Datei in welchem Paket liegt, steht in `manifest.json.gz` je Version (S3 Standard, ohne Restore lesbar).

## Entwicklung

```powershell
uv run pytest -q
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
