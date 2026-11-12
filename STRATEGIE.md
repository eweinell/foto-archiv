# Foto-Archiv – Archivierungsstrategie

Stand: 2026-11-12 · Grundlage für das spätere Archivierungstool

## 1. Ausgangslage

- Quelle: `~/Pictures` (Windows: `C:\Users\<name>\Pictures`), 88 Serienordner, ~450 GB, ~73.500 Dateien
- Ordnerschema uneinheitlich: meist `JJJJ.Name` (`2026.Kenia`), aber auch `2007_pentax`, `2025`, `sortiert`
- Innerhalb einer Serie teils Unterordner nach Typ (`raws/`, `jpg/`, `clips/`, `pixel/`), teils gemischt
- Wachstum: 2024 65 GB, 2025 90 GB, 2026 bisher 173 GB, Planung mit **150–200 GB/Jahr**
- Backupmedien: **2 HDDs à ~1,5 TB**, werden unregelmäßig bespielt, Nachkauf möglich
- AWS: Bucket in **eu-west-1**, bisher **Jahresordner mit Tars** in Deep Archive, „gelegentlich gepflegt“
- Bearbeitung mit RawTherapee (`.pp3`), Verwaltung mit digiKam (`digikam4.db`, `recognition.db`)
- **RAWs werden später voraussichtlich nicht erneut entwickelt.** Sie sind also lokal entbehrlich, sobald die Serie fertig ist.

Bestand nach Klasse (Messung 2026-11-12):

| Klasse | ≤ 2024 | ab 2025 | ohne Jahr | Summe |
|---|---|---|---|---|
| raw | 36 GB | 197 GB | 0,3 GB | 233 GB |
| img-original | 103 GB | 12 GB | 19 GB | 133 GB |
| img-derived | 9 GB | 42 GB | 0,1 GB | 51 GB |
| video | 13 GB | 13 GB | 3 GB | 28 GB |
| sidecar | | | | 0,3 GB (18.000 Dateien) |
| Cache/Müll | | | | ~4 GB |

## 2. Dateiklassen

Die Zuordnung richtet sich nach **Dateiendung und Dateiname, nicht nach dem Ordner**.

| Klasse | Regel | Wert |
|---|---|---|
| `raw` | `.arw .pef .cr2 .cr3 .nef .dng .raf .orf .rw2` sowie die Sidecars `.pp3 .xmp .dop .pto` | Original, wird aber nicht mehr entwickelt |
| `img-derived` | `.jpg .jpeg .tif .tiff .png .heic`, **wenn** es in derselben Serie ein RAW mit gleichem Basisnamen gibt (Suffixe wie `-1`, `_edit` werden ignoriert) | **Endprodukt**; nach dem Ausdünnen die einzige lokale Fassung |
| `img-original` | Bildendungen ohne passendes RAW (Handy, alte Kameras) | **unersetzlich** |
| `video` | `.mp4 .mov .mpg .avi .mts .m4v` | unersetzlich, groß |
| `other` | alles Übrige (`.xcf`, `.kdenlive`, `.wav`, `.pdf` …) | gering, wird mitgesichert |
| `exclude` | `Thumbs.db`, `thumbnails-digikam.db`, `desktop.ini`, `.directory`, `.dtrash/`, `*.dtrashinfo` | wird nicht gesichert |

Sidecars gehören zur Klasse `raw`, weil sie ohne die RAWs nutzlos sind.

Die Kataloge `digikam4.db` und `recognition.db` werden **gesondert** gesichert: klein, versioniert, in S3 Standard.

## 3. Ebenen und Regeln pro Klasse

| | Laptop | HDD A (zu Hause) | HDD B (außer Haus) | S3 Deep Archive |
|---|---|---|---|---|
| Rolle | Ansehen und Bearbeiten | erstes Archiv, wird bei jedem Lauf bespielt | zweite Kopie, wird z. B. 2× im Jahr nachgezogen | Kopie für den Katastrophenfall, unveränderlich |

| Klasse | lokal behalten? | Bedingung für lokales Löschen | Ziel-Kopien |
|---|---|---|---|
| `raw` | **nein**, nach Abschluss der Serie | S3 und ≥ 1 HDD, beide geprüft | S3 + 2 HDD |
| `video` | optional | S3 und ≥ 1 HDD, beide geprüft | S3 + 2 HDD |
| `img-derived` | **ja** | – | S3 + 2 HDD |
| `img-original` | **ja** | – | S3 + 2 HDD |

Eine Serie gilt als **abgeschlossen**, wenn sie Exporte (`img-derived`) enthält und seit ≥ 60 Tagen unverändert ist. Das Tool schlägt solche Serien vor, die Bestätigung ist manuell.

Wirkung: Lokal werden bis zu ~230 GB RAW frei, mit Videos bis zu ~260 GB.

## 4. Archivformat

### Einheit: Serie × Klasse, als Tar-Pakete

- Je Serie und Klasse entstehen unkomprimierte Tars, aufgeteilt in **Pakete ≤ 4 GB** (FAT32-sicher, über einen Konfigurationswert änderbar).
- Die Pfade im Tar sind relativ zu `~/Pictures`. Entpacken in `~/Pictures` stellt also die ursprüngliche Struktur wieder her.
- **Auf der HDD und in S3 liegen identische Pakete.** Sie werden einmal auf HDD A gepackt, dann nach HDD B kopiert und von HDD A nach S3 hochgeladen. Auf dem Laptop wird dafür kein Zwischenspeicher gebraucht.
- Einzelne Dateien von der HDD holen: über das Manifest das Paket finden, dann `tar -xf raw-003.tar "2026.Kenia/raws/DSC01234.ARW"`.

### Warum nicht mehr Tars pro Jahr?

Pakete pro Serie lassen sich übers Jahr verteilt versiegeln. Man muss nicht bis zum Jahresende warten oder ein Jahres-Tar neu hochladen. Außerdem lassen sich einzelne Serien und Klassen gezielt wiederherstellen. **Das Jahr bleibt als oberste Ordnerebene erhalten**, so passt die neue Struktur zum bisherigen Aufbau.

### Struktur (gleich auf HDD und in S3)

```
# in S3 unter dem Präfix fotos/ (neben dem Altbestand fotos/<Jahr>/*.tar)
<Jahr>/<Serie>/v<N>/manifest.json.gz      # S3: Klasse STANDARD (lesbar ohne Restore)
<Jahr>/<Serie>/v<N>/raw-001.tar           # S3: DEEP_ARCHIVE
<Jahr>/<Serie>/v<N>/img-derived-001.tar
<Jahr>/<Serie>/v<N>/img-original-001.tar
<Jahr>/<Serie>/v<N>/video-001.tar
<Jahr>/<Serie>/v<N>/other-001.tar
_kataloge/<Datum>/digikam4.db, recognition.db   # S3: STANDARD
_index/index-<UTC-Zeit>.sqlite.gz                # Index-Schnappschuss je push-s3, S3: STANDARD
                                                 # (HDD: _index/index.sqlite)
```

- Jahr: aus dem Präfix des Ordnernamens. Ohne Präfix (`sortiert`, `Camera` …) liegt die Serie unter `_ohne-jahr/`.
- `v1` ist die erste Versiegelung. Spätere Änderungen (neue Exporte, neue `.pp3`) kommen in `v2` usw., und zwar **nur als Nachtrag**. In S3 wird nichts überschrieben oder gelöscht: Das vermeidet die Gebühr für vorzeitiges Löschen (Mindestlaufzeit 180 Tage) und bewahrt den Verlauf.

### Manifest (je Serie und Version)

```json
{
  "series": "2026.Kenia", "year": 2026, "version": 1, "sealed": "2026-11-20T10:00:00Z",
  "bundles": [{"name": "raw-001.tar", "class": "raw", "size": 4290000000, "sha256": "…", "files": 290}],
  "files": [{"path": "2026.Kenia/raws/DSC01234.ARW", "class": "raw", "size": 24500000,
             "mtime": "2026-02-14T08:12:00Z", "sha256": "…", "bundle": "raw-001.tar"}]
}
```

Der Gesamtindex (lokal, auf jeder HDD und in S3) hält pro Serie, Version und Paket fest, **wo** es liegt (HDD-ID, S3-Key) und wann es zuletzt geprüft wurde. HDDs werden über eine Markierungsdatei `foto-archiv/volume.json` (feste ID plus änderbares Label) erkannt, unabhängig vom Laufwerksbuchstaben.

## 5. Bestehender Bestand (Altbestand)

- **Bisherige AWS-Tars bleiben unangetastet.** Sie werden im Index als `legacy` geführt (Key, Größe, Datum). Ihre Inhalte sind unbekannt, solange man sie nicht wiederherstellt.
- Optional: die Altbestands-Tars Jahr für Jahr per **Bulk-Restore** einmalig zurückholen, mit `tar -tvf` ein Manifest erzeugen und mit dem lokalen Bestand abgleichen. Das kostet wenige Cent pro Jahr, wenn man unter 100 GB Download pro Monat bleibt, und beweist nebenbei, dass sich die Daten wiederherstellen lassen.
- Danach entscheiden:
  - Alte Jahre bleiben im Altbestand, wenn sie vollständig abgedeckt sind. Sie sind klein; ein ganzes Jahr wiederherzustellen kostet Cent.
  - Sonst werden sie im neuen Format neu gepackt.
  - Doppelt gespeicherte Daten kosten < 5 $/Jahr. Altbestand erst löschen, wenn er **> 180 Tage** alt ist und die neue Fassung geprüft wurde.
- Die bisher offen abgelegten Ordner auf den HDDs bleiben bestehen, bis die Tars daneben liegen und geprüft sind. Danach können sie entfernt werden, wenn Platz gebraucht wird.

## 6. Kapazität der HDDs

- Heute ~450 GB, also passt alles auf jede der beiden 1,5-TB-Platten (~30 % belegt).
- Bei 150–200 GB/Jahr sind sie in **~4–5 Jahren** voll. Das Tool warnt ab 80 % Belegung.
- Dann eine neue Generation kaufen (z. B. 2 × 4–5 TB, ~110–140 € je Platte, Richtwert) und den gesamten Bestand umkopieren. Die alten Platten werden zur dritten Kopie oder ausgemustert.
- Grundsatz: **Jede Platte enthält den vollständigen Bestand** und ist dank Index und Manifesten selbstbeschreibend. Eine Verteilung über mehrere Platten ist erst nötig, wenn das Archiv größer als die größte verfügbare Platte wird.
- Prüfung: **Einmal im Jahr** pro Platte alle Paket-Hashes gegen den Index prüfen und die SMART-Werte ansehen.

## 7. Kosten (eu-west-1, USD, Stand 11/2026)

| Posten | Preis |
|---|---|
| Deep Archive Speicher | 0,00099 $/GB-Monat (≈ 1 $/TB-Monat), mindestens 180 Tage |
| Hochladen nach Deep Archive | ~0,05 $ / 1.000 Objekte |
| Verwaltungsaufwand pro Objekt | 40 KB (32 KB zum Deep-Archive-Preis, 8 KB zum Standard-Preis) |
| Abruf Bulk (≤ 48 h) / Standard (≤ 12 h) | 0,003 $/GB / 0,02 $/GB |
| Download ins Internet | 0,09 $/GB, **erste 100 GB/Monat frei** |
| Wiederhergestellte Kopie (temporär) | 0,023 $/GB-Monat anteilig für die Restore-Tage |

| Laufende Kosten | heute (0,45 TB) | in 5 Jahren (~1,5 TB) |
|---|---|---|
| S3 Deep Archive | ~5 $/Jahr | ~18 $/Jahr |
| HDD (vorhanden) | 0 € | Neue Generation ~5–6 €/TB/Jahr pro Kopie |

| Wiederherstellung | Daten | Bulk | Standard | HDD |
|---|---|---|---|---|
| Eine Serie, nur Exporte (Kenia) | 10 GB | 0,03 $ | 0,20 $ | 0 €, Minuten |
| Eine Serie komplett (Kenia) | 86 GB | 0,26 $ | 1,72 $ | 0 € |
| Alles heute, auf einmal | 450 GB | ~33 $ | ~40 $ | – |
| Alles heute, über 5 Monate verteilt | 450 GB | ~1,50 $ | ~9 $ | – |
| Alles in 5 Jahren | 1,5 TB | ~130 $ | ~155 $ | – |

Der größte Kostenfaktor ist der Download, nicht der Abruf. Wiederherstellung immer nach Klasse filtern und standardmäßig `Bulk` verwenden.

## 8. Sicherheit

- Das Tool bekommt einen eigenen IAM-Benutzer, beschränkt auf den Archiv-Bucket, mit `s3:PutObject`, `s3:GetObject`, `s3:RestoreObject` und `s3:ListBucket`, **ohne `DeleteObject`**.
- Versionierung aktivieren, wahlweise Object Lock im Governance-Modus. Das schützt vor versehentlichem Löschen und vor Ransomware.
- Verschlüsselung: Die serverseitige Standardverschlüsselung (SSE-S3) genügt. Clientseitige Verschlüsselung (z. B. `age`) ist optional, erhöht aber das Risiko, die Daten durch einen verlorenen Schlüssel nicht mehr wiederherstellen zu können.
- **Wiederherstellung ohne das Tool muss immer möglich bleiben:** `aws s3api restore-object` → `aws s3 cp` → `tar -xf`.

## 9. Tool – Funktionsumfang (Python + boto3)

| Befehl | Funktion | Risiko |
|---|---|---|
| `scan` | Bestand erfassen, Klassen zuordnen, Serien und Jahre ermitteln, Änderungen gegenüber dem Index erkennen | nur lesend |
| `status` | pro Serie: neu, geändert, abgeschlossen, versiegelt; Kopien (HDD A/B, S3); Kandidaten zum Ausdünnen | nur lesend |
| `seal <serie>` | Manifest und Tars auf der angeschlossenen HDD erzeugen und prüfen | schreibt auf HDD |
| `copy-hdd` | fehlende Pakete von HDD zu HDD kopieren und prüfen | schreibt auf HDD |
| `push-s3` | fehlende Pakete von der HDD nach S3 hochladen (Deep Archive) inkl. Manifest, Prüfsummen vergleichen | schreibt nach S3 |
| `verify` | HDD gegen Index prüfen (Hashes), S3 gegen Index prüfen (Existenz, Größe, Prüfsumme) | nur lesend |
| `slim` | RAW (und optional Video) lokal löschen, **nur** wenn die Bedingungen aus Abschnitt 3 erfüllt sind; Trockenlauf als Standard | löscht lokal |
| `restore <serie> [--class …] [--tier bulk]` | Pakete von der HDD entpacken oder aus S3 abrufen, abwarten, herunterladen, entpacken | schreibt lokal |
| `legacy scan` | Altbestands-Keys in den Index aufnehmen (Inhaltsabgleich per Restore: geplant) | nur lesend |

Reihenfolge der Umsetzung: `scan` → `status` → `seal` → `push-s3` → `copy-hdd`/`verify` → `slim` → `restore` → `legacy`.
