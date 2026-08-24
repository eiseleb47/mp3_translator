#!/usr/bin/env bash
# Startet die GUI aus dem projekteigenen venv.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$DIR/.venv/bin/python" ]]; then
  echo "Kein venv gefunden. Bitte zuerst './setup.sh' ausführen." >&2
  exit 1
fi
cd "$DIR"
exec "$DIR/.venv/bin/python" -m audio_transkript "$@"
