"""Kopiert Audiodateien in einen tmp-Ordner und wandelt sie via ffmpeg in 16-kHz-Mono-WAV um."""

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

AUDIO_EXTENSIONS = [
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
    ".wma", ".amr", ".3gp", ".3gpp", ".mp4", ".mkv", ".webm", ".aiff", ".caf",
]


class AudioError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def safe_stem(name: str) -> str:
    stem = Path(name).stem.strip()
    stem = re.sub(r"[\\/\x00-\x1f]", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem or "Aufnahme"


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def stage_file(source: Path, workdir: Path) -> Path:
    """Kopiert die Quelldatei (z.B. vom Handy via MTP) in den Arbeitsordner."""
    target = unique_path(workdir, safe_stem(source.name), source.suffix.lower() or ".bin")
    try:
        shutil.copyfile(source, target)
        stat = source.stat()
        os.utime(target, (stat.st_atime, stat.st_mtime))
    except OSError as exc:
        raise AudioError(f"Datei konnte nicht kopiert werden: {exc}") from exc
    if target.stat().st_size == 0:
        raise AudioError("Datei ist leer (0 Bytes).")
    return target


def duration_seconds(path: Path) -> float | None:
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)["format"]["duration"]
        seconds = float(value)
    except (ValueError, KeyError, TypeError):
        return None
    return seconds if seconds > 0 else None


def to_wav(source: Path, target: Path) -> Path:
    result = _run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
    ])
    if result.returncode != 0 or not target.exists() or target.stat().st_size <= 44:
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"ffmpeg beendet mit Code {result.returncode}"
        raise AudioError(f"Audio konnte nicht dekodiert werden: {message}")
    return target


_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})(?!\d)"),
)


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    today = date.today()
    if found.year < 2000 or found > today:
        return None
    return found


def _date_from_name(name: str) -> date | None:
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(name):
            found = _valid_date(*(int(g) for g in match.groups()))
            if found:
                return found
    return None


def _date_from_metadata(path: Path) -> date | None:
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        return None
    try:
        raw = json.loads(result.stdout)["format"]["tags"]["creation_time"]
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return _valid_date(stamp.year, stamp.month, stamp.day)


def source_date(original_name: str, media_path: Path) -> date | None:
    """Aufnahmedatum: erst aus dem Dateinamen (WhatsApp: PTT-JJJJMMTT-WAxxxx), dann Metadaten, dann mtime."""
    found = _date_from_name(original_name)
    if found:
        return found
    found = _date_from_metadata(media_path)
    if found:
        return found
    try:
        stamp = datetime.fromtimestamp(media_path.stat().st_mtime)
    except OSError:
        return None
    return _valid_date(stamp.year, stamp.month, stamp.day)


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "unbekannt"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
