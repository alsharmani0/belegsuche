# Stand der Belegsuche

Bedienung und Messbefehle stehen in der [README](../README.md). Die früheren Messwerte sind unten
getrennt aufgeführt: Die beiden alten Fragenlisten sind kein Blindtest für den aktuellen Code.

## winmail.dat aus Outlook (TNEF) (16.11.2026)

Eine Vermessung des echten Bestands (alle Mails nur gelesen) ergab: knapp 1 % der Mails tragen eine `winmail.dat`,
in den jüngsten Vorgangsordnern ein Drittel; darin stecken vor allem PDFs, von denen es viele sonst nirgends gibt.

- **Lesen:** eigenes Modul `belegsuche/tnef.py`, nur Standardbibliothek (MS-OXTNEF): Attribute mit Prüfsumme,
  MAPI-Eigenschaften (benannte Eigenschaften, Mehrfachwerte, PT_OBJECT), Anhänge mit Daten, Name (lange Form vor
  dem Titel, der Umlaute oft nur als „?“ hat), MIME-Tag, Content-ID, Kennzeichen, Methode; eingebettete
  Nachrichten und OLE-Objekte. Mailtext aus PR_BODY, PR_HTML oder PR_RTF_COMPRESSED (LZFu mit CRC, MELA; aus HTML
  oder Text gekapseltes RTF wird entkapselt, sonst schlichter RTF-zu-Text-Weg ohne Hilfsprogramm). Codepages,
  die Python nicht als `cpNNNNN` kennt (28591, 28605, 20127), werden abgebildet.
- **Teile:** Die `winmail.dat` bleibt ein Teil ohne Seiten (`kind 'tnef'`, Hinweis „ausgewertet: 3 Anhänge“ oder
  der Grund). Jeder Anhang darin ist ein eigener Teil und läuft durch dieselbe Verarbeitung wie ein Mail-Anhang
  (Art an den ersten Bytes, Logos und zu kleine Bilder übersprungen – Bild im Mailtext heißt hier: Content-ID,
  Kennzeichen 4 oder versteckt –, Grenzen, gleicher Inhalt in derselben Mail nur einmal, Übernahme bekannter Seiten
  über die sha256, Warten gleichzeitiger Mails auf denselben Inhalt). Eingebettete Outlook-Nachrichten sind wie
  angehängte Mails ein Teil `mail` mit eigener Seite; eine `winmail.dat` in der `winmail.dat` wird mit geöffnet
  (Tiefe 2).
- **Adresse:** `mime_path` „2!3“ = Anhang 3 der `winmail.dat` in Teil 2 (Reihenfolge im Strom, übersprungene Logos
  mitgezählt), auch verschachtelt („2!3!1“, „3/2!1“). `find_part` löst das auf; Öffnen und Ausliefern über den
  Server sind unverändert (sha-Prüfung mit 409, Typ-Allowlist, Quarantäne, Namensbereinigung).
- **Mailtext:** Hat die Mail keinen brauchbaren Text (weniger als drei Buchstaben oder Ziffern), gilt der Text der
  `winmail.dat`; sonst wird nichts doppelt gespeichert. Im Bestand hat jede dieser Mails eigenen Text.
- **Robustheit:** Der Leser wirft nie; Längen werden gegen den Rest geprüft, Attribute (100.000), Eigenschaften je
  Liste (20.000), Anhänge (1.000) und entpacktes RTF (4 MB) sind begrenzt. Kaputte oder abgeschnittene Ströme
  liefern das Lesbare, der Rest steht als Hinweis am Teil; eine falsche Prüfsumme ist nur ein Hinweis.
- **Erkennung:** `sniff` prüft die TNEF-Kennung jetzt vor der Suche nach „%PDF-“ hinter einem Vorspann – eine
  kleine `winmail.dat` mit einem PDF vorn galt sonst als PDF.
- **Anzeige:** Suche, `belegsuche suche` und Oberfläche zeigen „Anhang 3 · Rechnung.pdf (aus winmail.dat)“; die
  Logzeile zählt „2 Anhänge (1 aus winmail.dat)“; der Bericht zählt die `winmail.dat` getrennt (geöffnet, Anhänge
  darin, nicht ausgewertet mit Grund).

**Verifiziert:** 829 Tests grün, davon 64 neue in `tests/test_tnef.py` und `tests/test_winmail.py` (nur erfundene
Daten: Bauhelfer für TNEF, LZFu und MELA in `tests/conftest.py`, Testvektoren aus MS-OXRTFCP, kaputte und
abgeschnittene Ströme, riesige Längenfelder, Kompressionsbombe, Umstellung 10 → 11, Öffnen mit sha-Prüfung).
Probelauf mit echten Daten (derselbe Nummernbereich wie bei der Nachbesserung: 2.540 Dateien, davon 1.366 Mails,
eigener Index, `--parallel 3`, Domus nur gelesen): erst mit dem alten Stand als Format 10 eingelesen (4,9 min,
171 Mails mit `winmail.dat`, alle „nicht ausgewertet“), dann mit dem neuen Stand umgestellt und eingelesen: genau
diese 171 Mails neu gelesen (0,1 min), die Seiten aller übrigen Dateien unverändert, 0 Fehler. In den
`winmail.dat` stecken 145 Anhänge (98 PDF, 46 Bilder, 1 Word) mit 306 Seiten; 49 enthalten nur Logos, 12 gar
keine Anhänge. 218 Seiten mit Texterkennung wurden übernommen, 72 neu erkannt – keine davon mit schon bekanntem
Inhalt; die Seitenbereiche sind lückenlos. Suche nach den zwei seltensten Wörtern jedes PDFs aus einer
`winmail.dat`: 93 von 98 unter den ersten 10 Treffern (59 auf Platz 1, 41 davon unter einer gleichen
eigenständigen Datei als „auch unter“), die übrigen auf Platz 11–12. Ein zweiter Lauf liest nichts mehr; die
alte Version lehnt den umgestellten Index ab. Größe, Änderungszeit und sha256 aller 2.545 Dateien des Bereichs
waren vorher und nachher gleich.

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

Offen (bewusst): Anhänge in `winmail.dat` (TNEF) blieben nach der Vorgabe unausgewertet (inzwischen umgesetzt,
siehe oben). Lange URLs und wiederkehrende Signaturbilder bleiben im
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
- **Rückfall:** Hängt eine Datei, wird sie zuerst einmal allein wiederholt (Zeitlimit 2 min). Hängt eine Mail
  wieder oder ist es der letzte Versuch nach Abstürzen, wird sie nur mit Mailtext gelesen (Hinweis am Eintrag),
  statt als Fehler zu enden. `index` meldet solche Mails, `--fehler-wiederholen` liest sie ganz nach. Ein Stand
  „nur Mailtext“ wird nie übernommen (weder beim Wiederholen noch für eine gleiche Kopie). Stapelspuren der
  Hänger: `zeitlimit.log` neben der Index-Datei. Anlass: Im ersten Lauf über alle Domus-Mails hingen 2 von
  42.951 Mails je 10 min, die allein in 1–2 s durchgingen.
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
Datum und den Anhangnamen in die Pfadspalte des Suchindex. Indexformat 11 öffnet `winmail.dat` (Teil `tnef`,
Anhänge darin mit `mime_path` „…!k“). Ein Index im Format 9 oder 10 wird ohne Neuaufbau umgestellt (nur das Format
wird vermerkt; vorhandene Suchzeilen ändern sich nicht). Dabei – und vor jedem `belegsuche index`, damit auch Mails
eines noch laufenden älteren Programms erfasst werden – werden genau die fertigen Mails zum Neueinlesen vorgemerkt,
deren `winmail.dat` Format 10 nur vermerkt hat (`parts.note` „nicht ausgewertet (winmail.dat aus Outlook)“). Ihr
alter Stand wird nicht übernommen, auch nicht von einer gleichen Mail an anderem Ort; die schon erkannten Seiten
ihrer übrigen Anhänge übernimmt das Neulesen aus ihren eigenen Teilen, ohne neue Texterkennung. Bis dahin bleibt
ihr bisheriger Text durchsuchbar. Noch ältere Suchindizes werden aus ihren gespeicherten Seitentexten neu aufgebaut
– auch bei bereits indizierten Dateien, die auf das erneute Einlesen warten. Bestehende OCR-Ergebnisse werden bei
diesen Umstellungen nicht ersetzt. Ältere Programmversionen lehnen einen Index im Format 11 ab.

## Bekannte Grenzen

- Excel, ZIP und Outlook-`.msg` werden nicht gelesen; der Bericht zählt sie als „nicht unterstützt“. In Mails
  bleiben Excel-, ZIP- und Video-Anhänge, Anhänge über 50 MB und passwortgeschützte PDFs unausgewertet (Hinweis am
  Anhang). Verschlüsselte Mails (S/MIME): nur der Kopf.
- `winmail.dat`: über 100 MB nicht geöffnet; OLE-Objekte und Verweis-Anhänge darin werden nicht ausgewertet (nur
  ein Hinweis), ebenso verschlüsselte Outlook-Nachrichten (S/MIME); bei Kalendereinladungen werden nur die Anhänge
  gelesen, nicht die Termindaten. Eingebettete Nachrichten und `winmail.dat` in der `winmail.dat` bis Tiefe 2. Der
  Text der `winmail.dat` zählt nur, wenn die Mail keinen eigenen hat. Die Nachrichtenattribute (Absender, Datum)
  der `winmail.dat` werden nicht ausgewertet; Kopf und Datum kommen aus der Mail.
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
