# Stand der Belegsuche

Bedienung und Messbefehle stehen in der [README](../README.md). Die früheren Messwerte sind unten
getrennt aufgeführt: Die beiden alten Fragenlisten sind kein Blindtest für den aktuellen Code.

## Nachbesserung nach der Prüfung von E-Mail und Word (16.11.2026)

Zwei Prüfungen (Code/Sicherheit und Probelauf mit echten Daten) haben Befunde geliefert; behoben sind:

- **Hänger nach Absturz (kritisch, schon vor E-Mail/Word):** Stürzte ein Arbeitsprozess ab, während der
  Hauptprozess die nächste Datei einreihte, hing der Lauf für immer (der Pool beendete die übrigen Prozesse mit
  SIGTERM, das sie ignorieren) oder brach mit `BrokenProcessPool` ab. Der Pool beendet seine Prozesse jetzt mit
  SIGKILL; ein kaputter Pool beim Einreihen gilt als Absturz der laufenden Dateien.
- **Doppelte Texterkennung:** Mails, deren Anhang gerade in einer anderen Mail oder als Datei erkannt wird, warten
  darauf und übernehmen dann den Text (Probelauf: etwa 30 % der frisch erkannten Anhangseiten waren doppelt).
- **Suche nach Dateinamen:** Die Zeilen „Anhänge: …“ und „Anhang in DOMUS abgelegt: …“ werden angezeigt, zählen
  aber nicht als Mailtext; die Namen stehen abgewertet in der Pfadspalte (ausgewertete Anhänge auf ihren eigenen
  Seiten). Liegt die Fundstelle im Mailtext und verweist die Mail auf eine Datei, die fast genauso gut passt
  (Platzhalter oder gleicher Anhang), steht die Datei vorn und die Mail unter „auch unter: … (Mail mit Verweis)“.
- **Öffnen:** Anhang-Kopien tragen `com.apple.quarantine` (Word: geschützte Ansicht). Word-Originale öffnen als
  schreibgeschützte Kopie im System-Temp (keine `~$`-Sperrdatei im Domus-Ordner, kein Speichern ins Original).
  DOMUS-Platzhalter werden im Treffer genannt und öffnen die verlinkte Datei. `belegsuche start` räumt die Kopien
  auch beim Schließen des Terminals (SIGHUP/SIGTERM) weg; Temp-Dateien der Arbeitsprozesse liegen im Ordner des
  Laufs und verschwinden mit ihm. Liegt TMPDIR in einem eingelesenen Ordner, wird nicht eingelesen.
- **Mails:** Mails ohne Date-Kopf haben kein Ersatzdatum mehr im Suchtext und in `mails.sent_at`; der Treffer
  zeigt „(Dateidatum)“. Rohe Latin-1-Kopfzeilen und falsch als UTF-8 deklarierte Texte werden richtig gelesen.
  Fehlende MIME-Grenze: kein base64 im Suchtext. Tief verschachtelte Mails: nur der Kopf statt Fehler. Angehängte
  Mails mit base64-Kodierung werden gelesen. Der Hinweis „verschlüsselt“ einer angehängten Mail steht an deren
  Teil. Übernommene Seiten, die am Seitenlimit gekürzt werden, tragen einen Hinweis und werden nicht weiter
  übernommen. Eine gleiche Mail an anderem Ort verweist nur auf Dateien neben sich.
- **Typerkennung:** BMP und PDF-mit-Vorspann werden strenger erkannt („BMW …“ als Text ist kein Bild).
- **Hauptprozess:** Der Vorabblick auf die Anhänge zerlegt Mails ohne die Kopfzeilen-Objekte von `policy.default`
  (gleiche sha256 auf 1.500 echten Mails geprüft, präparierte Kopfzeilen 0,1 s statt 5 s); reine Textmails werden
  gar nicht zerlegt.
- **Word:** Strict-OOXML, als `.doc` gespeichertes HTML/Word-2003-XML/Text werden gelesen; Feldbefehle aus
  `textutil` (`SYMBOL 215 \f "Symbol"`, `TIME \@ "…"`, `MERGEFIELD …`) werden entfernt.
- **Anzeige und Bericht:** „Scan“ bei Mail-Treffern nur, wenn die Fundseite erkannt wurde; die Logzeile zählt
  übernommene OCR-Seiten getrennt; doppelte Anhänge derselben Mail zählen im Bericht als „doppelt“.

**Verifiziert:** 765 Tests grün, davon 48 neue in `tests/test_mail_review.py` (erfundene Daten; 43 davon
schlagen auf dem Stand vor der Nachbesserung fehl). Probelauf mit echten Daten (ein Nummernbereich mit 2.540
Dateien, davon 1.366 Mails, eigener Index, Domus nur gelesen): 132 s, 0 Fehler, 0 Abstürze, kein Original
verändert; 2.689 statt 2.791 Seiten per Texterkennung, kein Anhanginhalt mehr doppelt erkannt; Suche nach dem
Dateinamen eines Anhangs, der auch als Datei vorliegt: Datei auf Platz 1 in 28 von 34 Fällen (vorher 2 von 30),
bei Platzhaltern in 54 von 60 (vorher 6 von 40); keine Word-Seite mehr mit Feldbefehlen.

Offen (bewusst): Anhänge in `winmail.dat` (TNEF) bleiben nach der Vorgabe unausgewertet, obwohl dort oft PDFs
stecken; ein eigener TNEF-Leser wäre der nächste Schritt. Lange URLs und wiederkehrende Signaturbilder bleiben im
Index. Anhänge hinter einer falschen MIME-Grenze werden nicht gelesen (nur der Text bleibt, ohne base64).

## E-Mail und Word (16.11.2026)

- **Lesen:** `.eml` mit Kopf, Mailtext (HTML über `html.parser`, kein Netzzugriff) und Anhängen; Word über
  `zipfile` (docx/dotx/dotm) bzw. `textutil -stdin -stdout` (doc/dot/rtf/odt) mit eigenem Zeitlimit von 60 s.
  Anhänge werden an den ersten Bytes erkannt (PDF, PNG, JPEG, GIF, TIFF, HEIC, ZIP/docx, OLE/doc, RTF).
  PDF-Anhänge und Bilder werden im Speicher gelesen (Vision über `NSData`, auch HEIC), ohne Datei neben dem Original.
- **Seitenmodell:** `pages` bleibt fortlaufend (Seite 1 = Mailkopf + Mailtext, danach die Anhänge). Neue Tabellen
  `parts` (Teil, Seitenbereich, Art, Name, sha256, Lage im MIME-Baum, Hinweis) und `mails` (Kopf).
- **Sparen:** Der Hauptprozess bestimmt vor dem Einlesen die sha256 der Anhänge und gibt schon bekannte Seiten an
  den Arbeitsprozess. Mails werden nach den übrigen Dateien eines Laufs gelesen, damit Anhänge, die auch als
  Datei im Vorgangsordner liegen, übernommen werden. Umgekehrt übernimmt eine Datei die Seiten eines gleichen
  Anhangs.
- **Rückfall:** Nach Zeitlimit oder beim letzten Versuch nach Abstürzen wird eine Mail nur mit Mailtext gelesen
  (Hinweis am Eintrag), statt als Fehler zu enden.
- **Suche:** Treffer in Mails nennen Teil, Seite im Teil, Betreff, Absender und Datum. Seiten verschiedener Anhänge
  stärken sich nicht gegenseitig. Derselbe Inhalt als eigene Datei und als Mail-Anhang ist ein Treffer
  („auch unter: …eml (Anhang 1)“).
- **Öffnen:** „Mail öffnen“ startet `open -b com.apple.mail <eml>` (sonst das Standardprogramm), „Im Finder
  zeigen“ zeigt die `.eml`. „Anhang öffnen“ liest die Mail nur lesend neu, findet den Teil über `parts.mime_path`,
  prüft die sha256 (Abweichung: 409 „Mail geändert – neu einlesen“) und schreibt ihn in ein eigenes
  `mkdtemp(prefix="belegsuche-anhang-")` unter `$TMPDIR` (0700, Datei 0400, `O_EXCL|O_NOFOLLOW`). Liegt der
  Temp-Ordner in einem eingelesenen Ordner, wird nichts geschrieben. Dateiname: nur Basisname, ohne Steuer- und
  Richtungszeichen, ohne führende Punkte, gekürzt, Endung aus den ersten Bytes (HTML-Anhänge werden `.txt`).
  Geöffnet werden nur PDF, Bilder, doc/docx/rtf/odt und Text. `/datei/<id>/teil/<n>` liefert PDF und Bilder aus
  dem Speicher (für `#page=`); Mails, Word und alle übrigen Typen gehen nur als Download mit CSP `sandbox` hinaus.

**Verifiziert:** 717 Tests grün (lokal, Python 3.12.14, macOS, mit Apples Texterkennung), davon 130 neue mit
erfundenen Mails und Word-Dateien (69 zum Öffnen). Lesetest auf einem echten Bestand, nur lesend und ohne
Texterkennung: eine Stichprobe von 10.000 Mails und 400 Word-Dateien ohne Fehler, Mail im Median 4 ms, docx
2 ms, doc 49 ms.
Inhalt und Änderungszeit der gelesenen Dateien blieben unverändert. Das Öffnen ist mit erfundenen Mails
geprüft (`tests/test_mail_open.py`: Befehle, Temp-Kopie außerhalb der Ordner, Namensbereinigung, sha-Abweichung,
abgelehnte Typen, Download statt Anzeige, fremde Seiten, Aufräumen, XSS über Betreff und Anhangname); die
Oberfläche wurde im Browser hell, dunkel und in Handybreite angesehen.

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

Indexformat 10 ergänzt die Tabellen `parts` und `mails` und schreibt bei E-Mails Betreff, Absender, Empfänger,
Datum und den Anhangnamen in die Pfadspalte des Suchindex. Ein Index im Format 9 wird ohne Neuaufbau umgestellt
(nur das Format wird vermerkt; vorhandene Suchzeilen ändern sich nicht). Noch ältere Suchindizes werden aus ihren
gespeicherten Seitentexten neu aufgebaut – auch bei bereits indizierten Dateien, die auf das erneute
Einlesen warten. Bestehende OCR-Ergebnisse werden bei dieser Migration nicht ersetzt. Ältere Programmversionen
lehnen einen Index im Format 10 ab.

## Bekannte Grenzen

- Excel, ZIP und Outlook-`.msg` werden nicht gelesen; der Bericht zählt sie als „nicht unterstützt“. In Mails
  bleiben `winmail.dat` (Outlook/TNEF – im Probelauf enthielten mehr als die Hälfte davon PDFs), Excel-, ZIP- und
  Video-Anhänge, Anhänge über 50 MB und passwortgeschützte PDFs unausgewertet (Hinweis am Anhang). Verschlüsselte
  Mails (S/MIME): nur der Kopf.
- Bilder in Word-Dateien werden nicht per Texterkennung gelesen (eingescannte Belege in Word bleiben ohne Text,
  mit Hinweis). Seiten einer Word-Datei gibt es nur an harten Seitenumbrüchen; sonst ist sie eine Seite.
- Zitierte Vorgänger-Mails bleiben im Mailtext und werden mit durchsucht.
- Keine Suche nach Bedeutung, nur Synonyme, Wortteile und Tippfehler.
- Ein Umlautfehler zusammen mit einem Buchstabendreher im selben Wort wird nicht gefunden.
- Handschrift wird nur teilweise erkannt, auch mit Tesseract.
- Echte Mehrdeutigkeit: Jahresabrechnungen nennen dieselben Begriffe wörtlich, während der gesuchte Beleg andere
  Wörter benutzt. Sie stehen dann vor dem gesuchten Beleg.
- „Mail öffnen“ übergibt die `.eml` an Apple Mail. Je nach Einstellung lädt Mail dabei Bilder aus dem Netz
  (Zählpixel). Eine eigene, sichere Mailansicht in der Belegsuche gibt es noch nicht. Geöffnete Anhänge liegen bis
  zum Beenden der Suchoberfläche als Kopie im Temp-Ordner.
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
