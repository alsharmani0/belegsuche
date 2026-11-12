#!/bin/zsh
# Doppelklick: Suchoberfläche im Browser öffnen
cd "$(dirname "$0")" || exit 1
if [[ ! -x .venv/bin/belegsuche ]]; then
  echo "Belegsuche ist noch nicht installiert. Siehe README.md, Abschnitt Installation." >&2
  exit 1
fi
exec .venv/bin/belegsuche start
