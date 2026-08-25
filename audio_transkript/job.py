"""Ablauf eines Transkriptionslaufs: Dateien im Audioordner sichern, umwandeln, transkribieren, .docx schreiben."""

import atexit
import functools
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import unescape

from . import audio, config, docx_writer
from .transcriber import Cancelled, Transcriber


WORKDIR_PREFIX = "audio_transkript_"

_XML_TAG = re.compile(r"<[^>]*>")
# python-docx macht aus Tab/Zeilenumbruch im Text eigene Elemente – zurueckuebersetzen,
# sonst faellt das Zeichen beim Tag-Entfernen ersatzlos weg.
_W_TAB = re.compile(r"<w:tab\s*/?>")
_W_BR = re.compile(r"<w:br\s*/?>")
_SOURCE_LINE = re.compile(r"Quelle:\s*(.+?)\s*·\s*Dauer:", re.DOTALL)


class JobError(RuntimeError):
    pass


def _ensure_dir(path: Path, label: str) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JobError(f"{label} konnte nicht angelegt werden: {exc}") from exc
    return path


def sweep_stale_workdirs(max_age_hours: float = 12.0) -> int:
    """Arbeitsordner aufraeumen, die ein harter Abbruch (SIGKILL, Absturz) hinterlassen hat."""
    cutoff = time.time() - max_age_hours * 3600.0
    removed = 0
    try:
        leftovers = list(Path(tempfile.gettempdir()).glob(f"{WORKDIR_PREFIX}*"))
    except OSError:
        return 0
    for leftover in leftovers:
        try:
            if leftover.is_dir() and leftover.stat().st_mtime < cutoff:
                shutil.rmtree(leftover, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def collect_audio_files(source: Path, cancel) -> list[Path]:
    """Alle Audiodateien unterhalb von source – WhatsApp legt sie in Wochenordnern ab."""
    endings = {ext.lower() for ext in audio.AUDIO_EXTENSIONS}
    found: list[Path] = []
    for root, dirs, names in os.walk(source, onerror=lambda _exc: None):
        if cancel.is_set():
            break
        dirs.sort()
        for name in sorted(names):
            if Path(name).suffix.lower() in endings:
                found.append(Path(root) / name)
    return found


def _docx_source(path: Path) -> str | None:
    """Quelldateiname aus der Kopfzeile eines Transkripts ('Quelle: … · Dauer: …')."""
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("word/document.xml").decode("utf-8", "replace")
    except Exception:
        # Verschluesselt, beschaedigt, fremdes Format – eine unlesbare Datei darf den Lauf
        # nicht abbrechen, sie zaehlt einfach als "noch nicht transkribiert".
        return None
    text = _W_BR.sub("\n", _W_TAB.sub("\t", raw))
    match = _SOURCE_LINE.search(_XML_TAG.sub("", text))
    if match is None:
        return None
    return unescape(match.group(1), {"&quot;": '"', "&apos;": "'"}) or None


def transcribed_sources(output_dir: Path, cancel=None) -> dict[str, str]:
    """Bereits transkribierte Quelldateien im Zielordner: Quellname -> Name des Transkripts."""
    known: dict[str, str] = {}
    try:
        documents = sorted(output_dir.rglob("*.docx"))
    except OSError:
        return known
    for document in documents:
        if cancel is not None and cancel.is_set():
            break  # bei vielen Transkripten dauert der Durchlauf sonst spuerbar
        if document.name.startswith("~$"):  # Sperrdatei von Word/OnlyOffice
            continue
        source = _docx_source(document)
        if source and source not in known:
            known[source] = document.name
    return known


def run_copy_job(source_dir: str, target_dir: str, emit, cancel) -> dict:
    """Sichert Sprachnachrichten aus dem WhatsApp-Ordner im Audioordner am PC."""
    source = Path(source_dir).expanduser()
    target = Path(target_dir).expanduser()

    if not source.is_dir():
        raise JobError(f"WhatsApp-Ordner nicht erreichbar:\n{source}\n\nHandy angesteckt und entsperrt?")
    if source == target or target.is_relative_to(source):
        raise JobError("Quell- und Zielordner dürfen nicht ineinander liegen.")
    _ensure_dir(target, "Audioordner")

    emit("status", text="Suche Sprachnachrichten am Handy…")
    found = collect_audio_files(source, cancel)
    if cancel.is_set():
        return {"copied": [], "skipped": 0, "failed": [], "cancelled": True, "target": str(target)}
    emit("log", text=f"{len(found)} Audiodatei(en) im WhatsApp-Ordner gefunden.")

    copied: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped = 0
    total = max(1, len(found))

    for index, item in enumerate(found, start=1):
        if cancel.is_set():
            return {"copied": copied, "skipped": skipped, "failed": failed,
                    "cancelled": True, "target": str(target)}
        emit("status", text=f"Sichere {index}/{len(found)}: {item.name}")
        emit("progress", value=index / total * 100.0)
        stem, suffix = Path(item.name).stem, item.suffix
        destination = target / item.name
        try:
            size = item.stat().st_size
            if audio.existing_copy(target, stem, suffix, item, size) is not None:
                skipped += 1
                continue
            if destination.exists():
                present = destination.stat().st_size
                if present < size and audio.same_head(item, destination, present):
                    emit("log", text=f"↻ {item.name} war unvollständig – wird erneut kopiert")
                else:
                    # Gleicher Name, anderer Inhalt: die vorhandene Sicherung bleibt bestehen.
                    destination = audio.unique_path(target, stem, suffix)
                    emit("log", text=f"↻ {item.name} gibt es schon mit anderem Inhalt → {destination.name}")
            shutil.copyfile(item, destination)
            written = destination.stat().st_size
            if size and written != size:
                destination.unlink(missing_ok=True)
                raise OSError(f"nur {written} von {size} Bytes übertragen")
            try:
                os.utime(destination, (item.stat().st_atime, item.stat().st_mtime))
            except OSError:
                pass  # nur die Datums-Rückfallebene, die Kopie selbst ist in Ordnung
            copied.append(item.name)
        except OSError as exc:
            failed.append((item.name, str(exc)))
            emit("log", text=f"✗ {item.name}: {exc}")

    emit("progress", value=100.0)
    return {"copied": copied, "skipped": skipped, "failed": failed,
            "cancelled": False, "target": str(target)}


def run_job(files, cfg: dict, emit, cancel, transcriber: Transcriber | None = None) -> dict:
    if not audio.ffmpeg_available():
        raise JobError(
            "ffmpeg wurde nicht gefunden. Bitte setup.sh ausführen oder "
            "'sudo apt install ffmpeg' nachholen."
        )

    sources = [Path(f) for f in files]
    if not sources:
        return {"written": [], "failed": [], "skipped": [], "cancelled": False}

    transcriber = transcriber or Transcriber()
    output_dir = _ensure_dir(Path(cfg["output_dir"]).expanduser(), "Zielordner")

    emit("status", text="Prüfe, was schon transkribiert wurde…")
    known = transcribed_sources(output_dir, cancel)
    if cancel.is_set():
        return {"written": [], "failed": [], "skipped": [], "cancelled": True}
    pending: list[Path] = []
    skipped: list[str] = []
    for source in sources:
        already = known.get(docx_writer.xml_safe(source.name))
        if already:
            skipped.append(source.name)
            emit("log", text=f"⏭ {source.name} – bereits transkribiert ({already})")
        else:
            pending.append(source)
    if skipped:
        emit("log", text=f"{len(skipped)} Datei(en) übersprungen, {len(pending)} verbleiben.")
    if not pending:
        emit("progress", value=100.0)
        return {"written": [], "failed": [], "skipped": skipped, "cancelled": False}

    archive_dir = _ensure_dir(config.audio_dir(), "Audioordner")
    total = len(pending)
    written: list[Path] = []
    failed: list[tuple[str, str]] = []
    cancelled = False
    # Nur die 16-kHz-WAVs sind Wegwerfware; die Audiodateien selbst bleiben im Audioordner.
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX))
    # Greift auch dann, wenn das Fenster mitten im Lauf geschlossen wird: der Worker
    # ist ein Daemon-Thread und wird beim Interpreter-Shutdown abgeschnitten, das
    # finally unten also uebersprungen. atexit laeuft davor noch.
    cleanup = functools.partial(shutil.rmtree, workdir, ignore_errors=True)
    atexit.register(cleanup)

    try:
        # Zuerst alles sichern, damit das Handy danach abgesteckt werden kann.
        staged: list[tuple[Path, Path]] = []
        for index, source in enumerate(pending, start=1):
            if cancel.is_set():
                cancelled = True
                break
            emit("status", text=f"Bereite vor {index}/{total}: {source.name}")
            try:
                staged.append((source, audio.archive_file(source, archive_dir)))
            except (audio.AudioError, OSError) as exc:
                failed.append((source.name, str(exc)))
                emit("log", text=f"✗ {source.name}: {exc}")

        if staged and not cancelled:
            emit("log", text=f"{len(staged)} Datei(en) bereit in {archive_dir}")
            if not transcriber.is_loaded(cfg["model"]):
                emit("status", text=f"Lade Modell '{cfg['model']}' (beim ersten Mal Download)…")
                try:
                    transcriber.load(cfg["model"])
                except Cancelled:
                    raise
                except Exception as exc:
                    raise JobError(f"Modell konnte nicht geladen werden: {exc}") from exc

        done = 0
        for source, copy in staged:
            if cancel.is_set():
                cancelled = True
                break
            label = source.name
            emit("status", text=f"Transkribiere {done + 1}/{len(staged)}: {label}")
            try:
                duration = audio.duration_seconds(copy, cancel=cancel)
                wav = audio.to_wav(
                    copy, audio.unique_path(workdir, copy.stem, ".16k.wav"), cancel=cancel
                )

                def on_progress(fraction: float, base=done) -> None:
                    emit("progress", value=(base + fraction) / len(staged) * 100.0)

                result = transcriber.transcribe(
                    wav, cfg["model"], cfg["language"], duration,
                    on_progress=on_progress, cancel=cancel,
                )
                recorded = audio.source_date(label, copy, cancel=cancel)
                stem = recorded.strftime("%d.%m.%Y") if recorded else audio.safe_stem(label)
                target = audio.unique_path(output_dir, stem, ".docx")
                docx_writer.write_docx(
                    target, stem, result, label,
                    cfg["model"], bool(cfg.get("timestamps")),
                )
                written.append(target)
                words = sum(len(text.split()) for _, _, text in result["segments"])
                emit("log", text=f"✓ {target.name}  ({words} Wörter)")
            except (Cancelled, audio.AudioCancelled):
                cancelled = True
                break
            except Exception as exc:
                if cancel.is_set():
                    cancelled = True
                    break
                failed.append((label, str(exc)))
                emit("log", text=f"✗ {label}: {exc}")
            finally:
                done += 1
                emit("progress", value=done / max(1, len(staged)) * 100.0)
    finally:
        atexit.unregister(cleanup)
        shutil.rmtree(workdir, ignore_errors=True)

    return {"written": written, "failed": failed, "skipped": skipped, "cancelled": cancelled}
