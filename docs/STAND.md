# Stand der Belegsuche

Messwerte, bekannte Grenzen und offene Punkte nach den Prüfrunden vom 27./28.09.2026
(Commits 38b7d0f, 2ff2489, e01438d). Bedienung und Messbefehle stehen in der [README](../README.md).

## Messwerte

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

Seit 2ff2489 gilt Indexformat 8 (vorher 7). Ein bestehender Index wird beim ersten Aufruf nach dem Update einmalig
aus den gespeicherten Seitentexten neu aufgebaut, ohne neue Texterkennung (Meldung „Suchindex wird auf das neue
Format umgestellt …“).

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
2. **Apple gegen Tesseract:** bisher nur bei Handschrift verglichen; ein systematischer Vergleich auf echten Scans
   fehlt (README, Abschnitt „Suchqualität messen“).
3. **Große Bestände über 100.000 Seiten:** Suchzeit nach den Korrekturen nicht erneut gemessen.
4. **Originalschutz unter echten Bedingungen:** exFAT-SSD, sehr große Ordner, lange Läufe mit `--parallel`.
5. **Zeitlimit beim Einlesen:** Ob 10 Minuten je Seite auf dem Zielgerät passen (große Scans, schwache Hardware),
   ist nicht gemessen.
