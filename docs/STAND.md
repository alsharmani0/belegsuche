# Stand der Belegsuche

Bedienung und Messbefehle stehen in der [README](../README.md). Die früheren Messwerte sind unten
getrennt aufgeführt: Die beiden alten Fragenlisten sind kein Blindtest für den aktuellen Code.

## Projektprüfung vom 11.11.2026

- **Einlesen:** Ein vorübergehend nicht lesbarer Dateieintrag gilt nicht als gelöscht. Ein während der
  Verarbeitung geänderter Beleg bleibt für den nächsten Lauf offen. Nach Massenumbenennungen werden belegte
  Gegenstücke bereinigt; fehlende Dateien ohne entsprechendes Gegenstück bleiben geschützt.
- **Index und Exporte:** Fremde oder neuere Datenbanken werden nicht als eigener Index initialisiert.
  Leseoperationen auf dem aktuellen Format benötigen keine schreibende Verbindung. Alte Seitentexte bleiben
  bei der Migration auch für offene Aktualisierungen auffindbar. CSV-/JSON-Exporte ersetzen weder die
  Quelldateien noch die Datenbank; ein fehlgeschlagener Export lässt den alten Bericht bestehen.
- **Texterkennung:** Transparente Bilder werden auf weißem Hintergrund gelesen. Fehler bei der Prüfung
  eingebetteter PDF-Bilder und stark beschädigte Textebenen lösen einen OCR-Versuch aus. Ein identischer
  zweiter OCR-Durchgang bei schwachen Bildresultaten entfällt.
- **Suche und Oberfläche:** Hausnummernlisten erfinden keine Zwischenwerte mehr. Die Bewertung großer
  Sammel-PDFs vermeidet wiederholtes lineares Suchen nach der Position derselben Seite. Die Qualitätsauswertung
  meldet ungültige CSV-Zeilen, statt Fragen aus der Quote verschwinden zu lassen. Oberfläche und Bericht
  zählen weiterhin auffindbare ältere Texte einheitlich; beim Leeren der Suche bleiben keine alten Treffer stehen.
- **Testwerkzeuge:** Determinismusprüfungen erzeugen zwei getrennte temporäre Bestände; ein bestehender
  Testkorpus wird nicht mehr gelöscht. Die Holdout-Fragen fließen standardmäßig nicht in Inhaltsdiagnosen ein.

**Verifiziert:** 416 bestehende Tests auf dem unveränderten Ausgangscommit, 580 bestandene Tests nach den
Korrekturen, lokal mit Python 3.12.14 auf macOS. Das Wheel wurde gebaut, isoliert installiert und mit dem
synthetischen Korpus ausgeführt: 26/26 Entwicklungs- und 16/16 Holdout-Fragen fanden die richtige Datei
auf Platz 1 und der erwarteten Seite. Inhalt und Änderungszeit der 64 Eingabedateien blieben unverändert.
Die installierte Oberfläche wurde im Browser mit Suche und anschließendem Leeren des Suchfelds überprüft.
Ein privater Suchindex wurde für diese Projektprüfung nicht geöffnet oder migriert.

## Historische Messwerte

- **Tests:** 384 grün (lokal auf macOS, mit Apples Texterkennung). Die Nachweise der letzten Korrekturen stehen in
  `tests/test_claude_fixes.py` und `tests/test_claude_runde3.py`.
- **Künstlicher Testbestand** (`testdata/`, 64 Dateien: Scans, Fotos, HEIC, Fax-TIFF, Sammel-PDF, kaputte
  Scanner-Textebenen, verschlüsselte und beschädigte PDFs): 26/26 Entwicklungs- und 16/16 zurückgehaltene
  Suchfragen auf Platz 1, jeweils auf der richtigen Seite. Zuletzt am 28.09.2026 nachgemessen.
- **Echte Belege**, zwei Sätze mit je 30 Suchfragen (gemessen vor den Prüfrunden vom 27./28.09.):
  - erster Satz: 25/30 auf Platz 1, 29/30 unter den ersten 5;
  - zweiter Satz: 22/30 auf Platz 1, 27/30 unter den ersten 5, 29/30 unter den ersten 20.

  Beide Sätze wurden für die Fehlersuche benutzt und sind nicht mehr blind. Die Fragen hat eine KI formuliert, die
  die Belege vor sich hatte; sie sind daher eher leichter als echte Suchen aus dem Gedächtnis.
- **Suchzeit:** auf einem echten Index mit ≈12.600 Seiten im Mittel ≈0,3 s, längste Anfrage ≈0,8 s. Bei 100.000
  synthetischen Seiten braucht jede Anfrageart unter 0,5 s. Bei 1 Mio. Seiten lagen Suchen bei 0,7–2 s, gemessen
  vor späteren Korrekturen und seitdem nicht wiederholt.

## Indexformat

Indexformat 9 trennt explizite Hausnummernlisten von Zahlenbereichen. Ältere Suchindizes werden aus ihren
gespeicherten Seitentexten neu aufgebaut – auch bei bereits indizierten Dateien, die auf das erneute
Einlesen warten. Bestehende OCR-Ergebnisse werden bei dieser Migration nicht ersetzt.

## Bekannte Grenzen

- Word, Excel, E-Mails (.msg/.eml) und ZIP werden nicht gelesen; der Bericht zählt sie als „nicht unterstützt“.
- Keine Suche nach Bedeutung, nur Synonyme, Wortteile und Tippfehler.
- Ein Umlautfehler zusammen mit einem Buchstabendreher im selben Wort wird nicht gefunden.
- Handschrift wird nur teilweise erkannt, auch mit Tesseract.
- Echte Mehrdeutigkeit: Jahresabrechnungen nennen dieselben Begriffe wörtlich, während der gesuchte Beleg andere
  Wörter benutzt. Sie stehen dann vor dem gesuchten Beleg.
- Die Suchoberfläche hat kein Passwort. Sie ist nur auf diesem Mac erreichbar (127.0.0.1) und lehnt Anfragen mit
  fremder `Origin`/`Referer` ab, andere Benutzerkonten auf demselben Mac könnten sie aber aufrufen, solange sie läuft.
- Das Zeitlimit beim Einlesen (10 Minuten ohne fertige Seite) gilt nur bei paralleler Verarbeitung, nicht bei
  `--parallel 1`.

## Offene Punkte

1. **Echte Suchqualität:** Ein neuer, unbenutzter Satz echter Suchfragen fehlt, am besten aus dem Gedächtnis
   gestellt. Die beiden bisherigen Sätze sind mit dem aktuellen Stand nicht nachgemessen; die Regeln aus 2ff2489
   (kurzes Wort vor Zahl, führende Nullen, getrennt geschriebene Straße) können dort Plätze verschieben.
2. **Apple gegen Tesseract:** Ein früherer Vergleich auf 25 echten Bild-PDFs (73 Seiten) prüfte 50 visuell
   abgelesene Kennungen, Daten und Beträge. Beide Engines lieferten alle 50 Werte; Apple verwendete bei einem
   Betrag einen Punkt statt des gedruckten Kommas. Das war kein Vergleich vollständiger Transkripte und kein
   Qualitätsnachweis für die inzwischen geänderte Extraktion.
3. **Große Bestände über 100.000 Seiten:** Suchzeit nach den Korrekturen nicht erneut gemessen.
4. **Originalschutz unter echten Bedingungen:** exFAT-SSD, sehr große Ordner, lange Läufe mit `--parallel`.
5. **Zeitlimit beim Einlesen:** Ob 10 Minuten je Seite auf dem Zielgerät passen (große Scans, schwache Hardware),
   ist nicht gemessen.
6. **Schreibweisen und Suchregeln:** „7/11“ wird weiterhin wie ein Bereich behandelt; „7, 11“ ist eine
   Aufzählung. Festlegen, was Schrägstriche im tatsächlichen Bestand bedeuten. Eigene Synonymgruppen mit
   Mehrwort-Ausdrücken greifen vom Einzelwort zur Phrase; die umgekehrte Suche ist nicht symmetrisch.
   Eine Entscheidung über Phrasenerkennung und Gewichtung fehlt.
7. **Vollständigkeit oder Laufzeit:** Unveränderte Größe und identische gespeicherte Änderungszeit lösen
   keinen neuen Hashvergleich aus. Ein optionaler vollständiger Integritätslauf würde auch solche Ersetzungen
   erkennen, kostet aber einen Lesedurchgang über den Bestand. Auch das gezielte erneute Erkennen bereits
   eingelesener Scans wäre eine eigene Option; heute geht das mit einem separaten neuen Index.
8. **Gemeinsam genutzte Macs:** Festlegen, ob der lokale Server zusätzlich eine Sitzungsauthentisierung
   braucht. Für einen einzelnen Benutzer ist das eine andere Abwägung als für mehrere Benutzerkonten.
9. **Kleine API-/Bediengrenzen:** Der Aufruf des macOS-Programms `open` hat kein eigenes Zeitlimit.
   Außerdem verwenden Browser und Server numerische Datei-IDs: JavaScript kann oberhalb von
   `2^53 - 1` nicht mehr jede ID exakt darstellen; der Downloadpfad lässt höchstens 18 Ziffern zu.
   Für normale, automatisch vergebene IDs ist das praktisch unerheblich. Falls importierte IDs dieser
   Größe unterstützt werden sollen, ist ein einheitlicher String- oder Wertebereichsvertrag nötig.
