# foto-archiv

Archiviert Medien-Serien klassenweise (RAW, Exporte, Original-Bilder, Video, Audio) als Tar-Pakete
auf Archiv-HDDs und nach S3 Glacier Deep Archive. Mehrere Quellordner (Fotos, Videos, Musik …) sind
als **Sammlungen** möglich, jede mit eigenem Ordner im Archiv. Hintergrund und Kosten:
[STRATEGIE.md](STRATEGIE.md).

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
foto-archiv init --bucket mein-foto-bucket   # Konfiguration anlegen (Sammlung "fotos" = ~/Pictures)
foto-archiv collections              # Sammlungen und ihr Bestand
foto-archiv scan                     # Bestand aller Sammlungen erfassen (nur lesend)
foto-archiv status                   # Übersicht je Serie
foto-archiv status 2026.Kenia        # Details (eindeutiger Name genügt, sonst fotos/2026.Kenia)
foto-archiv status musik/            # alle Serien einer Sammlung

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
foto-archiv restore videos/2024.Hochzeit --staging F:\  # Zwischenablage auf externer Platte (große Videos)

foto-archiv legacy scan              # Altbestand im Bucket auflisten
foto-archiv catalog backup           # digiKam-Datenbanken sichern
```

Konfiguration: `%LOCALAPPDATA%\foto-archiv\config.toml`, Index: `%LOCALAPPDATA%\foto-archiv\index.sqlite`
(eine Kopie des Index liegt auf jedem Volume unter `foto-archiv\_index\` und als Schnappschuss in S3,
dazu auf jedem Volume eine `LIESMICH.txt` mit der Ablagestruktur).

## Sammlungen (Fotos, Videos, Musik …)

Jeder Quellordner ist eine Sammlung mit eigenem Ordner im Archiv, auf allen HDDs und in S3 gleich:

```toml
[[collections]]
id = "fotos"                         # steht im Index - nicht mehr ändern
source_root = "C:/Users/ich/Pictures"
archive_dir = "fotos"                # Ordnername im Archiv; änderbar (gilt für neue Versionen)
profile = "photo"                    # RAW/Export-Abgleich

[[collections]]
id = "videos"                        # längere Videos: Diashows, Renderings
source_root = "D:/Videos"
profile = "media"                    # Klassen nur nach Endung: video, audio, img-original, other
settle_days = 120                    # optional: eigene Ruhezeit
exclude_dirs = ["proxy", "CacheClip"]  # optional: Ordnernamen in jeder Tiefe (zusätzlich zu den globalen)
exclude_paths = ["tmp", "2024.Hochzeit/veraltet", "*/Render-Cache"]  # optional: bestimmte Pfade samt Inhalt

[[collections]]
id = "musik"
source_root = "D:/Musik"
archive_dir = "Musik"
profile = "media"
year = "none"                        # keine Jahresebene: Musik/<Serie>/v1/…
```

- **Serie** = Ordner der ersten Ebene im Quellordner (Musik also z. B. je Interpret). Bei `year = "name"`
  (Standard) kommt das Jahr aus dem Ordnernamen (`2024.Hochzeit` → `videos/2024/2024.Hochzeit/`), sonst `_ohne-jahr/`.
  Ordner am besten **vor** dem ersten `seal` passend benennen - ein umbenannter Ordner ist eine neue Serie.
- Kurze Clips in einer Fotoserie bleiben in der Foto-Sammlung (Klasse `video`).
- Liegt der Quellordner einer Sammlung innerhalb eines anderen (z. B. `Pictures/Diashows`), gehört er nur zur inneren Sammlung.
- Ist ein Quellordner nicht verfügbar (externe Platte), überspringt `scan` die Sammlung; ihr Bestand gilt nicht als gelöscht.
- **Ausschließen:** `exclude_paths` sind Pfade relativ zum Quellordner (Ordner oder Datei, Groß-/Kleinschreibung egal);
  `*` und `?` gelten innerhalb eines Namens, `"*/veraltet"` trifft also `veraltet` direkt in jeder Serie. Für einen Namen in jeder Tiefe
  `exclude_dirs` / `exclude_names`. `foto-archiv scan --excluded` listet, was übergangen wurde, `collections` zeigt die Regeln.
  Wird nachträglich ausgeschlossen, verschwinden nie archivierte Einträge aus dem Index; bereits Archiviertes bleibt im
  Archiv (und per `restore` erreichbar), gilt lokal aber als nicht vorhanden.
- Pfade im Tar sind relativ zum Quellordner der Sammlung; `restore` entpackt standardmäßig dorthin.
- Archivplatten mit **NTFS oder exFAT** formatieren: Videos > 4 GiB ergeben Pakete > 4 GiB; auf FAT32 bricht `seal` vorher ab.
- Der Archivordner auf den HDDs heißt `foto-archiv` (`volume_dir` für neue Platten); umbenennen ist möglich,
  erkannt wird die Platte über `volume.json`.

### Umstellung eines bestehenden Archivs (einmalig)

```powershell
foto-archiv migrate --dry-run        # Vorschau
foto-archiv migrate                  # Index + Konfiguration (Sicherungen *.bak / index-schema1-*.sqlite) + angeschlossene HDDs
foto-archiv hdd migrate HDD-B        # weitere Platte, sobald angeschlossen (verschiebt <Jahr>/ nach fotos/<Jahr>/)
```

Die S3-Keys bleiben dabei unverändert: aus `prefix = "fotos/"` wird `prefix = ""` plus `archive_dir = "fotos"`.
Platten im alten Format werden bis zu ihrer Umstellung nicht benutzt.

Upload-Bandbreite dauerhaft festlegen, in der Konfiguration unter `[s3]`:

```toml
upload_limit = "2MB/s"                                    # fester Wert (leer = unbegrenzt)
upload_schedule = ["07:00-23:00=2MB/s", "23:00-07:00=0"]  # optional: je Tageszeit, 0 = unbegrenzt
```

Beim Zeitplan gilt das erste passende Fenster (lokale Uhrzeit), außerhalb aller Fenster `upload_limit`.
Wechsel greifen auch mitten in einem laufenden Upload. `push-s3 --limit` setzt für einen Lauf einen
festen Wert und ignoriert den Zeitplan. Die Grenze gilt gemeinsam für alle parallelen Teil-Uploads und
wird gleichmäßig im Datenstrom eingehalten (keine Lastspitzen).

## Wiederherstellung ohne Tool

```powershell
aws s3api restore-object --bucket mein-foto-bucket --key "fotos/2026/2026.Kenia/v1/raw-001.tar" `
    --restore-request '{\"Days\":7,\"GlacierJobParameters\":{\"Tier\":\"Bulk\"}}' --profile foto-archiv
# nach bis zu 48 h:
aws s3 cp "s3://mein-foto-bucket/fotos/2026/2026.Kenia/v1/raw-001.tar" . --profile foto-archiv
tar -xf raw-001.tar -C $HOME\Pictures     # in den Quellordner der Sammlung
```

Welche Datei in welchem Paket liegt, steht in `manifest.json.gz` je Version (S3 Standard, ohne Restore lesbar;
ab Format `@2` mit Angabe der Sammlung).

## Entwicklung

```powershell
uv run pytest -q
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
