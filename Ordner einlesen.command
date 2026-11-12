#!/bin/zsh
# Doppelklick: Ordner auswählen und einlesen (Abbruch mit Ctrl+C, beim nächsten Mal geht es weiter)
cd "$(dirname "$0")" || exit 1
if [[ ! -x .venv/bin/belegsuche ]]; then
  echo "Belegsuche ist noch nicht installiert. Siehe README.md, Abschnitt Installation." >&2
  exit 1
fi
# Nur bewusstes Abbrechen des Dialogs aktualisiert die bekannten Ordner.
DIR=$(osascript -e 'try' \
  -e 'POSIX path of (choose folder with prompt "Welchen Ordner soll die Belegsuche einlesen?")' \
  -e 'on error number -128' -e 'return ""' -e 'end try') || exit 1
if [ -z "$DIR" ]; then
  echo "Kein Ordner gewählt – bereits bekannte Ordner werden aktualisiert."
  .venv/bin/belegsuche index || exit $?
else
  .venv/bin/belegsuche index "$DIR" || exit $?
fi
echo
.venv/bin/belegsuche bericht || exit $?
echo
if [[ -t 0 ]]; then
  read "?Fertig. Enter drücken zum Schließen."
fi
