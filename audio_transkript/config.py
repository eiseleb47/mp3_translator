"""Persistente Einstellungen in ~/.config/audio-transkript/config.json."""

import json
import os
import re
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


def is_week_dir(name: str) -> bool:
    """WhatsApp legt Medien in Wochenordnern JJJJWW ab (z.B. 202634 = KW 34/2026)."""
    if len(name) != 6 or not name.isdigit():
        return False
    return 2000 <= int(name[:4]) <= 2999 and 1 <= int(name[4:]) <= 53


def _is_dir(path: Path) -> bool:
    """Path.is_dir() reicht vor Python 3.13 EACCES/ENOTCONN durch – etwa bei totem MTP-Mount."""
    try:
        return path.is_dir()
    except OSError:
        return False


def newest_week_dir(path: str) -> str:
    """Höchster Wochenordner unterhalb von path, sonst path selbst."""
    base = Path(existing_dir(path))
    try:
        weeks = [d for d in base.iterdir() if _is_dir(d) and is_week_dir(d.name)]
    except OSError:
        return str(base)
    return str(max(weeks, key=lambda d: d.name)) if weeks else str(base)


def existing_dir(path: str) -> str:
    """Nächster existierender Ordner (falls der gespeicherte Pfad z.B. abgesteckt wurde)."""
    candidate = Path(path).expanduser()
    while not _is_dir(candidate) and candidate != candidate.parent:
        candidate = candidate.parent
    return str(candidate)


def defaults() -> dict:
    home = Path.home()
    return {
        "start_dir": str(home),
        "output_dir": str(home / "Documents" / "audio_texte"),
        "model": "small",
        "language": "auto",
        "timestamps": False,
        "auto_newest": True,
    }


def _clean(raw: dict) -> dict:
    cfg = defaults()
    if not isinstance(raw, dict):
        return cfg
    for key in ("start_dir", "output_dir"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            cfg[key] = str(Path(value).expanduser())
    if raw.get("model") in MODELS:
        cfg["model"] = raw["model"]
    if raw.get("language") in {code for code, _ in LANGUAGES}:
        cfg["language"] = raw["language"]
    for key in ("timestamps", "auto_newest"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
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
