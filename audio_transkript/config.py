"""Persistente Einstellungen in ~/.config/audio-transkript/config.json."""

import functools
import json
import os
import subprocess
import tempfile
from pathlib import Path

APP_ID = "audio-transkript"

MODELS = ["tiny", "base", "small", "medium", "large-v3"]

LANGUAGES = [
    ("auto", "Automatisch erkennen"),
    ("de", "Deutsch"),
    ("en", "Englisch"),
    ("fr", "Französisch"),
    ("it", "Italienisch"),
    ("es", "Spanisch"),
    ("tr", "Türkisch"),
    ("hr", "Kroatisch"),
    ("sr", "Serbisch"),
    ("bs", "Bosnisch"),
    ("pl", "Polnisch"),
    ("ro", "Rumänisch"),
    ("ru", "Russisch"),
    ("ar", "Arabisch"),
]


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / APP_ID


def config_path() -> Path:
    return config_dir() / "config.json"


def is_dir(path) -> bool:
    """Path.is_dir() reicht vor Python 3.13 EACCES/ENOTCONN durch – etwa bei totem MTP-Mount."""
    try:
        return Path(path).expanduser().is_dir()
    except OSError:
        return False


def existing_dir(path: str) -> str:
    """Nächster existierender Ordner (falls der gespeicherte Pfad z.B. abgesteckt wurde)."""
    candidate = Path(path).expanduser()
    while not is_dir(candidate) and candidate != candidate.parent:
        candidate = candidate.parent
    return str(candidate)


@functools.lru_cache(maxsize=1)
def documents_dir() -> Path:
    """Lokalisierter Dokumentenordner – auf deutschen Systemen ~/Dokumente."""
    home = Path.home()
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    try:
        for line in (Path(base) / "user-dirs.dirs").read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOCUMENTS_DIR="):
                raw = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(home))
                candidate = Path(raw)
                if candidate.is_absolute() and candidate != home:
                    return candidate
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True, timeout=5
        )
        candidate = Path(result.stdout.strip())
        if result.returncode == 0 and candidate.is_absolute() and candidate != home:
            return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return home / "Documents"


def audio_dir() -> Path:
    """Fester Ablageort der Audiodateien – auf deutschen Systemen ~/Dokumente/audio_dateien."""
    return documents_dir() / "audio_dateien"


def defaults() -> dict:
    return {
        "whatsapp_dir": "",
        "output_dir": str(documents_dir() / "audio_texte"),
        "model": "small",
        "language": "auto",
        "timestamps": False,
    }


def _clean(raw: dict) -> dict:
    cfg = defaults()
    if not isinstance(raw, dict):
        return cfg
    value = raw.get("output_dir")
    if isinstance(value, str) and value.strip():
        cfg["output_dir"] = str(Path(value).expanduser())
    # Darf leer bleiben: solange nicht eingestellt, fragt der Knopf beim ersten Klick.
    value = raw.get("whatsapp_dir")
    if isinstance(value, str):
        cfg["whatsapp_dir"] = str(Path(value).expanduser()) if value.strip() else ""
    if raw.get("model") in MODELS:
        cfg["model"] = raw["model"]
    if raw.get("language") in {code for code, _ in LANGUAGES}:
        cfg["language"] = raw["language"]
    if isinstance(raw.get("timestamps"), bool):
        cfg["timestamps"] = raw["timestamps"]
    return cfg


def load() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as handle:
            return _clean(json.load(handle))
    except (OSError, ValueError):
        return defaults()


def save(cfg: dict) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(_clean(cfg), out, indent=2, ensure_ascii=False)
        os.replace(tmp, config_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
