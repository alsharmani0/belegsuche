# Testkorpus belegsuche

Synthetischer, vollständig **fiktiver** Korpus deutscher Hausverwaltungs- und Haushaltsbelege
(Handwerker, Hauswart/Hausmeister, Stadtwerke, Versicherung, Schornsteinfeger, Anwalt,
Kassenbons …) zum Testen von Indexierung, OCR und Suche.

Erzeugt mit `.venv/bin/python tools/make_testdata.py` (deterministisch, Seed 20260924; `testdata/korpus/` wird bei jedem Lauf gelöscht und neu erzeugt).
Prüfen mit `.venv/bin/python tools/check_testdata.py` (Optionen: `--determinism`, `--ocr`, `--render DIR`).

Alle Firmen, Personen, Adressen und Bankdaten sind erfunden. IBANs haben die Prüfziffer `00`
(ungültig), Web/E-Mail nutzen die reservierte TLD `.example`.

## Umfang

- 64 Dateien, 11.4 MB, 61 inhaltliche Belegseiten, 22 Firmen
- Zeitraum 2018–2025, Objekte: Musterstraße 12, Lindenweg 3, Am Hang 7, Birkenallee 21,
  Kastanienweg 5a, Mühlenstraße 40
- Verschachtelte, unordentliche Ordner (Umlaute, Leerzeichen, `&`, `+`), überwiegend
  nichtssagende Dateinamen (`scan0001.pdf` kommt dreimal vor, `Unbenannt.pdf` zweimal).

| kind | Anzahl | Bedeutung |
|---|---:|---|
| `digital_pdf` | 28 | PDF mit sauberem Textlayer |
| `scan_pdf` | 12 | reine Bild-PDF (Scan, verrauscht, schief, JPEG-Artefakte), kein Textlayer |
| `photo_jpg` | 4 | Handyfoto (Perspektive, Hintergrund, Licht) |
| `png` | 2 | PNG (Mail-Inlinebild bzw. Bildschirmfoto) |
| `heic` | 2 | HEIC-Handyfoto (via `sips`) |
| `tiff_multipage` | 1 | mehrseitiges TIFF, pro Seite ein anderer Beleg |
| `sammel_pdf` | 1 | mehrere Belege in einer PDF, digitale und gescannte Seiten gemischt |
| `garbage_textlayer_pdf` | 2 | Scan + unsichtbare, falsche OCR-Textebene |
| `encrypted_pdf` | 1 | passwortgeschützt – soll im Fehlerbericht landen |
| `corrupt_pdf` | 1 | abgeschnittene PDF – soll im Fehlerbericht landen |
| `unsupported` | 7 | xlsx/docx/msg/zip und Junk (.DS_Store, Thumbs.db, Office-Lockdatei) |
| `duplicate_of` | 3 | byte-identische Kopie einer anderen Datei |

## Schwierige Fälle

- **Scan-PDF um 90° gedreht**
  - `Buchhaltung/Eingang/Scans/scan0001.pdf`
- **Scan-PDF um 180° gedreht**
  - `Scans unsortiert/scan0012.pdf`
- **Thermo-Kassenbon (verblasst)**
  - `Buchhaltung/Eingang/Scans/scan0009.pdf`
  - `Kassenbons/scan0004.pdf`
  - `Kassenbons/IMG_6093.jpg`
- **Handyfoto mit EXIF-Orientierung 6**
  - `Handy Fotos/IMG_4402.jpg`
- **HEIC**
  - `Handy Fotos/IMG_5120.HEIC`
  - `Handy Fotos/IMG_7731.HEIC`
- **PNG**
  - `Mails/Anhänge/image001.png`
  - `Verschiedenes/Bildschirmfoto 2023-03-20 um 09.14.52.png`
- **Mehrseitiges TIFF (3 Seiten, 1-bit G4, Fax)**
  - `Buchhaltung/Eingang/Fax/FAX_20220207_0832.tif`
- **Sammel-PDF (6 Belege, Seiten 2/4/6 gescannt)**
  - `Buchhaltung/Eingang/Scan_Stapel_März.pdf`
- **Scan + unsichtbare Müll-Textebene**
  - `Buchhaltung/Eingang/Scans/scan0007.pdf`
  - `Scans unsortiert/Scan_2023-04-11.pdf`
- **Firmenname nur im Logo + Fußzeile**
  - `2021/Dokument.pdf`
  - `2024/Belege Jan-Apr/Dokument (4).pdf`
- **ASCII-Umlaute (Mueller, Strasse)**
  - `Mails/Anhänge/Mai 2023/RE_2023-00588.pdf`
- **Abgekürzte Straße (Musterstr.)**
  - `2021/Objekte/Musterstraße 12/Rechnungen/Rechnung (1).pdf`
  - `Objekte/Musterstr. 12/Dach/Unbenannt 2.pdf`
- **Hauswart statt Hausmeister**
  - `2020/Rechnungen 2020/Dokument (1).pdf`
  - `2022/Reinigung + Pflege/Rechnung Juli.pdf`
  - `2025/Offene Posten/scan0001.pdf`
- **Verschlüsselt (Benutzerpasswort)**
  - `Mails/Anhänge/Kostennote_2024-0133_verschlüsselt.pdf`
- **Defekt (auf 40 % abgeschnitten)**
  - `Mails/Anhänge/INV-93307.pdf`
- **Nicht unterstützt / Junk**
  - `Buchhaltung/Nebenkosten 2023.xlsx`
  - `Buchhaltung/~$Nebenkosten 2023.xlsx`
  - `Mails/Anhänge/AW Rechnung Heizung.msg`
  - `Verschiedenes/Mietvertrag Entwurf.docx`
  - `Mails/Anhänge/Rechnungen_2022.zip`
  - `.DS_Store`
  - `Buchhaltung/Eingang/Scans/Thumbs.db`
- **Dubletten (byte-identisch)**
  - `Buchhaltung/Erledigt/2022/4711_22.pdf  (= 2022/Handwerker/Rechnung_Vogt.pdf)`
  - `Mails/Anhänge/Rechnung 4711-22.pdf  (= 2022/Handwerker/Rechnung_Vogt.pdf)`
  - `Mails/Anhänge/Beitragsrechnung.pdf  (= Versicherung & Recht/Nordlicht/Beitrag 2019.pdf)`

## manifest.json

Ein Eintrag pro Datei: `path` (relativ zu `korpus/`), `kind`, `pages`, `tags`, `readable`,
`expected_error` (`encrypted`, `corrupt`, `unsupported`, `junk` oder `null`), `duplicate_of`,
`sha256`, `size_bytes`, `params` (Degradierungsparameter), `password` (nur `encrypted_pdf`)
und `facts` – pro Seite: `seite`,
`quelle` (`textlayer`, `nur_bild`, `verschluesselt`, `defekt`), `doc_id`, `firma` (wie gedruckt),
`rechnungsnummer`, `datum` (ISO), `datum_text` (wie gedruckt), `gesamtbetrag` (Punkt-Dezimal),
`gesamtbetrag_text` (wie gedruckt), `leistung`, `objektadresse` (wie gedruckt, ggf. abgekürzt),
`objektadresse_normiert`.

Hinweis: Bei Stadtwerke-Jahresabrechnungen ist `gesamtbetrag` der Rechnungsbetrag brutto (vor
Abzug der Abschläge); die Nachzahlung steht zusätzlich im Dokument.

## Suchanfragen

`queries_dev.csv` und `queries_holdout.csv`: UTF-8, Semikolon, Kopfzeile `suche;datei;seite;notiz`.
`datei` ist das EINE erwartete Dokument (relativ zu `korpus/`), `seite` 1-basiert (bei
Einzelseiten immer 1). `notiz` beginnt mit der Kategorie: `exakt`, `name`, `beschreibend`, `schwer`.

- `queries_dev.csv`: 26 Anfragen – exakt 6, name 6, beschreibend 6, schwer 8
- `queries_holdout.csv`: 16 Anfragen – exakt 4, name 3, beschreibend 5, schwer 4

**Die Holdout-Datei ist für eine unabhängige Evaluation reserviert:** Suche und Ranking werden
nicht anhand dieser Anfragen abgestimmt. Sie zielt – bis auf TIFF und Sammel-PDF (andere Seiten) –
auf andere Dokumente als die Dev-Datei.

Dubletten: Eine Dev-Anfrage zielt auf eine Datei mit byte-identischen Kopien; laut `notiz`
zählt ein Treffer auf eine der Kopien ebenfalls als korrekt. Verschlüsselte, defekte und nicht
unterstützte Dateien sind nie Ziel einer Anfrage.
