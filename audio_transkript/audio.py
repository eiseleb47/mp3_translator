"""Sichert Audiodateien im Audioordner und wandelt sie via ffmpeg in 16-kHz-Mono-WAV um."""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path

AUDIO_EXTENSIONS = [
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
    ".wma", ".amr", ".3gp", ".3gpp", ".mp4", ".mkv", ".webm", ".aiff", ".caf",
]


class AudioError(RuntimeError):
    pass


class AudioCancelled(AudioError):
    """Der Kindprozess wurde wegen eines Abbruchs beendet – kein Fehler der Datei."""


FFMPEG_TIMEOUT = 900.0


def _kill(proc: subprocess.Popen) -> None:
    proc.terminate()
    for _ in range(2):
        try:
            proc.communicate(timeout=3)
            return
        except subprocess.TimeoutExpired:
            proc.kill()


def _run(cmd: list[str], timeout: float = FFMPEG_TIMEOUT, cancel=None) -> subprocess.CompletedProcess:
    """Wie subprocess.run, aber mit Zeitlimit und abbrechbar – sonst haengt der Worker unbegrenzt."""
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as exc:
        raise AudioError(f"{cmd[0]} konnte nicht gestartet werden: {exc}") from exc

    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.25)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            aborted = cancel is not None and cancel.is_set()
            if aborted or time.monotonic() >= deadline:
                _kill(proc)
                if aborted:
                    raise AudioCancelled(f"{cmd[0]} abgebrochen")
                raise AudioError(
                    f"{cmd[0]} hat nicht geantwortet (Zeitlimit {int(timeout)} s) – "
                    "Datei oder Laufwerk nicht erreichbar?"
                )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def safe_stem(name: str) -> str:
    stem = Path(name).stem.strip()
    stem = re.sub(r"[\\/\x00-\x1f]", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem or "Aufnahme"


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return True  # im Zweifel als belegt behandeln, statt etwas zu ueberschreiben


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while _exists(candidate):
        if counter > 999:
            raise AudioError(f"Zu viele gleichnamige Dateien in {directory}")
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


# Container-Kopf und erste Audioframes reichen, um zwei verschiedene Aufnahmen zu
# unterscheiden. Mehr zu lesen wuerde das Sichern vom Handy unnoetig ausbremsen.
PROBE_BYTES = 64 * 1024


def same_head(first: Path, second: Path, length: int) -> bool:
    """Stimmen die ersten Bytes beider Dateien überein? Trennt Dubletten von bloß Namensgleichen."""
    limit = min(max(0, length), PROBE_BYTES)
    if limit == 0:
        return False
    try:
        with open(first, "rb") as one, open(second, "rb") as two:
            return one.read(limit) == two.read(limit)
    except OSError:
        return False


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def existing_copy(directory: Path, stem: str, suffix: str, source: Path, size: int) -> Path | None:
    """Sucht 'stem.ext', 'stem (2).ext', … nach einer Sicherung derselben Datei ab."""
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while _exists(candidate):
        present = _size_of(candidate)
        # size == 0 melden manche MTP-Mounts auch für gefüllte Dateien – dann zaehlt nur der Inhalt.
        if present > 0 and size in (0, present) and same_head(source, candidate, present):
            return candidate
        if counter > 999:
            return None
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return None


def _in_dir(path: Path, directory: Path) -> bool:
    """Liegt path direkt in directory? Aufgeloest, damit Symlinks und '..' nicht taeuschen."""
    try:
        return path.parent.resolve() == directory.resolve()
    except OSError:
        return False


def archive_file(source: Path, archive_dir: Path) -> Path:
    """Legt die Quelldatei (z.B. vom Handy via MTP) im Audioordner ab und gibt den Pfad dort zurück."""
    try:
        # Vor dem Kopieren lesen: danach kann das Gerät bereits abgesteckt sein.
        stat = source.stat()
    except OSError as exc:
        raise AudioError(f"Datei nicht lesbar: {exc}") from exc

    if _in_dir(source, archive_dir):
        if stat.st_size == 0:
            raise AudioError("Datei ist leer (0 Bytes).")
        return source  # liegt schon im Audioordner – nichts zu kopieren

    stem = safe_stem(source.name)
    suffix = source.suffix.lower() or ".bin"
    found = existing_copy(archive_dir, stem, suffix, source, stat.st_size)
    if found is not None:
        return found  # identische Sicherung schon vorhanden
    target = unique_path(archive_dir, stem, suffix)

    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        raise AudioError(f"Datei konnte nicht kopiert werden: {exc}") from exc

    try:
        copied = target.stat().st_size
    except OSError as exc:
        raise AudioError(f"Kopie konnte nicht geprüft werden: {exc}") from exc

    if copied == 0 or (stat.st_size and copied != stat.st_size):
        try:
            target.unlink()  # keine halbe Sicherung im Audioordner zurücklassen
        except OSError:
            pass
        if copied == 0:
            raise AudioError("Datei ist leer (0 Bytes).")
        raise AudioError(
            f"Kopie unvollständig ({copied} von {stat.st_size} Bytes) – "
            "Verbindung zum Gerät unterbrochen?"
        )

    try:
        os.utime(target, (stat.st_atime, stat.st_mtime))
    except OSError:
        pass  # betrifft nur das Datum als Rückfallebene, die Kopie selbst ist in Ordnung
    return target


def duration_seconds(path: Path, cancel=None) -> float | None:
    try:
        result = _run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ], timeout=120.0, cancel=cancel)
    except AudioCancelled:
        raise
    except AudioError:
        return None
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)["format"]["duration"]
        seconds = float(value)
    except (ValueError, KeyError, TypeError):
        return None
    return seconds if seconds > 0 else None


def to_wav(source: Path, target: Path, cancel=None) -> Path:
    result = _run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
    ], cancel=cancel)
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


def _date_from_metadata(path: Path, cancel=None) -> date | None:
    try:
        result = _run([
            "ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
            "-of", "json", str(path),
        ], timeout=120.0, cancel=cancel)
    except AudioCancelled:
        raise
    except AudioError:
        return None
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


def source_date(original_name: str, media_path: Path, cancel=None) -> date | None:
    """Aufnahmedatum: erst aus dem Dateinamen (WhatsApp: PTT-JJJJMMTT-WAxxxx), dann Metadaten, dann mtime."""
    found = _date_from_name(original_name)
    if found:
        return found
    found = _date_from_metadata(media_path, cancel=cancel)
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
