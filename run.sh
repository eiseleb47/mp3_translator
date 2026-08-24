#!/usr/bin/env bash
# Startet die GUI aus dem projekteigenen venv; Fehler werden auch ohne Terminal sichtbar.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_error() {
  printf '%s\n' "$1" >&2
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Audio-Transkript" --no-wrap --text="$1" >/dev/null 2>&1
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --title "Audio-Transkript" --error "$1" >/dev/null 2>&1
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Audio-Transkript" "$1" >/dev/null 2>&1
  fi
}

if [[ ! -x "$DIR/.venv/bin/python" ]]; then
  show_error "Die Installation ist unvollständig – es wurde keine Arbeitsumgebung gefunden.

Bitte einmal im Projektordner ausführen:
    cd \"$DIR\"
    ./setup.sh"
  exit 1
fi

cd "$DIR"

if [[ -t 2 ]]; then
  # Im Terminal gestartet: Ausgaben wie gewohnt direkt anzeigen.
  "$DIR/.venv/bin/python" -m audio_transkript "$@"
  exit $?
fi

# Über Menüeintrag oder Schreibtisch-Symbol gestartet: stderr landet sonst im Nichts.
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/audio-transkript.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || LOG="/tmp/audio-transkript.log"
"$DIR/.venv/bin/python" -m audio_transkript "$@" 2>"$LOG"
rc=$?
if [[ $rc -ne 0 ]]; then
  cat "$LOG" >&2
  show_error "Audio-Transkript wurde unerwartet beendet (Code $rc).

$(tail -n 6 "$LOG" 2>/dev/null)

Vollständige Ausgabe: $LOG"
fi
exit $rc
