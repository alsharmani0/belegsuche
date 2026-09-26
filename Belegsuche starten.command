#!/bin/zsh
# Doppelklick: Suchoberfläche im Browser öffnen
cd "$(dirname "$0")"
exec .venv/bin/belegsuche start
