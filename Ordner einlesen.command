#!/bin/zsh
# Doppelklick: Ordner auswählen und einlesen (Abbruch mit Ctrl+C, beim nächsten Mal geht es weiter)
cd "$(dirname "$0")"
DIR=$(osascript -e 'POSIX path of (choose folder with prompt "Welchen Ordner soll die Belegsuche einlesen?")' 2>/dev/null)
if [ -z "$DIR" ]; then
  echo "Kein Ordner gewählt – bereits bekannte Ordner werden aktualisiert."
  .venv/bin/belegsuche index
else
  .venv/bin/belegsuche index "$DIR"
fi
echo
.venv/bin/belegsuche bericht
echo
read "?Fertig. Enter drücken zum Schließen."
