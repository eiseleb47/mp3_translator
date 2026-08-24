#!/usr/bin/env bash
# Installiert Systempakete, legt das venv an und richtet den Menüeintrag ein.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
SKIP_SYSTEM=0
PREFETCH=""
DESKTOP_ICON=1

usage() {
  cat <<'USAGE'
Verwendung: ./setup.sh [Optionen]

  --no-system-deps     Systempakete (ffmpeg, GTK3) nicht installieren
  --no-desktop-icon    Kein Symbol auf dem Schreibtisch anlegen
  --prefetch [MODELL]  Whisper-Modell sofort herunterladen (Standard: small)
  -h, --help           Diese Hilfe
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-system-deps) SKIP_SYSTEM=1; shift ;;
    --no-desktop-icon) DESKTOP_ICON=0; shift ;;
    --prefetch)
      if [[ -n "${2:-}" && "${2:-}" != -* ]]; then PREFETCH="$2"; shift 2; else PREFETCH="small"; shift; fi
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; usage; exit 1 ;;
  esac
done

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

SUDO=""
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

install_system_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    say "Systempakete via apt (Linux Mint / Ubuntu / Debian)"
    $SUDO apt-get update
    $SUDO apt-get install -y python3 python3-venv python3-gi gir1.2-gtk-3.0 ffmpeg gvfs-backends
  elif command -v pacman >/dev/null 2>&1; then
    say "Systempakete via pacman (Arch / Garuda / Manjaro)"
    $SUDO pacman -S --needed --noconfirm python python-gobject gtk3 ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    say "Systempakete via dnf (Fedora)"
    $SUDO dnf install -y python3 python3-gobject gtk3 ffmpeg gvfs-mtp
  elif command -v zypper >/dev/null 2>&1; then
    say "Systempakete via zypper (openSUSE)"
    $SUDO zypper install -y python3 python3-gobject typelib-1_0-Gtk-3_0 ffmpeg gvfs-backend-afc
  else
    warn "Unbekannte Distribution. Bitte manuell installieren: python3 (venv), PyGObject + GTK3, ffmpeg"
  fi
}

if [[ $SKIP_SYSTEM -eq 0 ]]; then
  install_system_deps
else
  say "Systempakete übersprungen"
fi

command -v python3 >/dev/null 2>&1 || die "python3 nicht gefunden."
python3 - <<'PY' || die "Python 3.9 oder neuer wird benötigt."
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

say "Virtuelle Umgebung: $VENV"
# --system-site-packages, weil PyGObject/GTK aus den Systempaketen kommt (kein pip-Wheel vorhanden).
if [[ -f "$VENV/pyvenv.cfg" ]] && ! grep -qi "include-system-site-packages *= *true" "$VENV/pyvenv.cfg"; then
  warn "Vorhandenes venv sieht keine Systempakete – wird neu angelegt."
  rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv --system-site-packages "$VENV" \
    || die "venv konnte nicht angelegt werden (Paket python3-venv fehlt?)."
fi

say "Python-Pakete installieren"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$DIR/requirements.txt"

say "Prüfung"
"$VENV/bin/python" - <<'PY' || die "Prüfung fehlgeschlagen – siehe Meldung oben."
import shutil, sys
problems = []
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa: F401
except Exception as exc:
    problems.append(f"PyGObject/GTK3 fehlt ({exc}). Auf Mint/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0")
for module in ("faster_whisper", "docx"):
    try:
        __import__(module)
    except Exception as exc:
        problems.append(f"{module} fehlt ({exc})")
for binary in ("ffmpeg", "ffprobe"):
    if shutil.which(binary) is None:
        problems.append(f"{binary} nicht im PATH")
for problem in problems:
    print("  ✗", problem, file=sys.stderr)
raise SystemExit(1 if problems else 0)
PY
echo "  ✓ GTK3, faster-whisper, python-docx, ffmpeg vorhanden"

chmod +x "$DIR/run.sh"

say "Symbol und Menüeintrag installieren"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
ICONS="$SHARE/icons/hicolor/scalable/apps"
mkdir -p "$ICONS"
cp "$DIR/assets/audio-transkript.svg" "$ICONS/audio-transkript.svg"

APPS="$SHARE/applications"
mkdir -p "$APPS"
LAUNCHER="$APPS/audio-transkript.desktop"
cat > "$LAUNCHER" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Audio-Transkript
Comment=Sprachnachrichten in Word-Dokumente umwandeln
Exec=$DIR/run.sh
Path=$DIR
Icon=audio-transkript
Terminal=false
StartupWMClass=Audio-Transkript
Categories=AudioVideo;Audio;
Keywords=Transkript;Whisper;WhatsApp;Sprachnachricht;
DESKTOP
chmod +x "$LAUNCHER"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$LAUNCHER" || warn "Menüeintrag meldet Auffälligkeiten (siehe oben)."
fi
echo "  ✓ $LAUNCHER"

if [[ $DESKTOP_ICON -eq 1 ]]; then
  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
  [[ -n "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"
  if [[ -d "$DESKTOP_DIR" ]]; then
    install -m 755 "$LAUNCHER" "$DESKTOP_DIR/audio-transkript.desktop"
    # Nemo/Nautilus starten .desktop-Dateien sonst nicht, sondern zeigen sie als Text.
    gio set "$DESKTOP_DIR/audio-transkript.desktop" metadata::trusted true 2>/dev/null || true
    echo "  ✓ $DESKTOP_DIR/audio-transkript.desktop"
  else
    warn "Schreibtisch-Ordner nicht gefunden ($DESKTOP_DIR) – Symbol übersprungen."
  fi
fi

mkdir -p "$HOME/Documents/audio_texte"

if [[ -n "$PREFETCH" ]]; then
  say "Modell '$PREFETCH' herunterladen"
  "$VENV/bin/python" - "$PREFETCH" <<'PY'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
print("  ✓ Modell im Cache (~/.cache/huggingface)")
PY
fi

say "Fertig. Start über das Anwendungsmenü ('Audio-Transkript') oder mit ./run.sh"
