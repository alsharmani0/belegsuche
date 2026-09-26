"""Synonyme und Füllwörter für die Suche.

Eigene Synonyme: Datei `synonyme.txt` neben der Index-Datenbank, eine Gruppe pro Zeile,
Begriffe durch Komma getrennt, z. B.:

    hausmeister, hauswart, objektbetreuer
    heizung, therme, kessel
"""
from __future__ import annotations

from pathlib import Path

from .normalize import words

DEFAULT_GROUPS = [
    "hausmeister, hauswart, hausmeisterdienst, hausmeisterservice, objektbetreuung, hausmeisterdienstleistung, hausmeisterdienstleistungen, hausmeistertaetigkeit",
    "heizung, heizungsanlage, therme, gastherme, kessel, heizkessel, brenner, brennwertkessel, brennwertgeraet",
    "wartung, inspektion, instandhaltung, wartungsvertrag",
    "reparatur, instandsetzung, stoerungsbeseitigung, notdienst",
    "nebenkosten, betriebskosten, nebenkostenabrechnung, betriebskostenabrechnung",
    "strom, elektrizitaet, stromrechnung",
    "wasser, trinkwasser, wasserversorgung, abwasser",
    "muell, abfall, abfallentsorgung, muellabfuhr, restmuell, abfallgebuehren, muellgebuehren",
    "reinigung, gebaeudereinigung, treppenhausreinigung, unterhaltsreinigung",
    "schornsteinfeger, kaminkehrer, bezirksschornsteinfeger, feuerstaettenschau",
    "versicherung, versicherungsschein, police, beitragsrechnung",
    "quittung, kassenbon, kassenbeleg, bon, beleg",
    "aufzug, fahrstuhl, lift, aufzugsanlage",
    "garten, gartenpflege, gruenpflege, grundstueckspflege",
    "maler, malerarbeiten, anstrich",
    "schluessel, schluesseldienst, schliessanlage",
    "anwalt, rechtsanwalt, kanzlei",
    "elektriker, elektro, elektroinstallation",
    "klempner, sanitaer, installateur",
    "dach, dachdecker, dachreparatur",
    "rechnung, faktura",
    "gutschrift, erstattung, rueckerstattung, guthaben, rueckzahlung",
    "mahnung, zahlungserinnerung",
    "therme, thermen, gastherme, gasthermen, heiztherme, kombitherme",
    "rauchmelder, rauchwarnmelder",
    "grundsteuer, grundbesitzabgaben",
]

# Schwächere Verwandtschaft (Gewicht 0.6 statt 0.75): ähnliche, aber nicht gleichbedeutende Begriffe
WEAK_GROUPS = [
    "aufstellung, uebersicht, auflistung, zusammenstellung, liste",
    "haus, haeuser, gebaeude, objekt, objekte, liegenschaft",
    "ueberweisung, ueberwiesen, zahlung, gezahlt, bezahlt, ausgezahlt, auszahlung",
    "bescheid, gebuehrenbescheid, abgabenbescheid",
]
SYN_WEIGHT, WEAK_WEIGHT = 0.75, 0.6

STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "einem", "einen",
    "und", "oder", "von", "vom", "fuer", "mit", "im", "in", "am", "an", "zu", "zum", "zur",
    "bei", "auf", "aus", "nach", "ueber", "unter", "the", "of", "and",
    # Umfangs-/Füllwörter ohne Inhalt ("alle Häuser", "meine Rechnung")
    "alle", "alles", "aller", "allen", "saemtliche", "jede", "jeder", "jedes", "mein", "meine", "meinen", "unser",
    "unsere", "welche", "welcher", "wo", "was", "wie", "bitte", "nur", "auch", "noch", "ohne", "bis", "seit",
    "ab", "als", "ist", "sind", "war", "wurde", "gibt", "hat", "habe",
}


def load_synonyms(extra_file: Path | None = None) -> dict[str, dict[str, float]]:
    """Wort -> {verwandtes Wort/Phrase: Gewicht}."""
    lines = [(line, SYN_WEIGHT) for line in DEFAULT_GROUPS] + [(line, WEAK_WEIGHT) for line in WEAK_GROUPS]
    if extra_file and extra_file.exists():
        for line in extra_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                lines.append((line.replace("=", ","), SYN_WEIGHT))
    table: dict[str, dict[str, float]] = {}
    for line, weight in lines:
        # mehrteilige Einträge ("e-check", "dguv prüfung") werden als Phrase gesucht
        group = {" ".join(words(w)) for w in line.split(",") if words(w)}
        for w in group:
            entry = table.setdefault(w, {})
            for other in group - {w}:
                entry[other] = max(entry.get(other, 0.0), weight)
    return table
