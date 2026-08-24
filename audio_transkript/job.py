"""Ablauf eines Transkriptionslaufs: Dateien nach /tmp kopieren, umwandeln, transkribieren, .docx schreiben."""

import atexit
import functools
import shutil
import tempfile
import time
from pathlib import Path

from . import audio, docx_writer
from .transcriber import Cancelled, Transcriber


WORKDIR_PREFIX = "audio_transkript_"


class JobError(RuntimeError):
    pass


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


def run_job(files, cfg: dict, emit, cancel, transcriber: Transcriber | None = None) -> dict:
    if not audio.ffmpeg_available():
        raise JobError(
            "ffmpeg wurde nicht gefunden. Bitte setup.sh ausführen oder "
            "'sudo apt install ffmpeg' nachholen."
        )

    sources = [Path(f) for f in files]
    total = len(sources)
    if total == 0:
        return {"written": [], "failed": [], "cancelled": False}

    transcriber = transcriber or Transcriber()
    output_dir = Path(cfg["output_dir"]).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JobError(f"Zielordner konnte nicht angelegt werden: {exc}") from exc

    written: list[Path] = []
    failed: list[tuple[str, str]] = []
    cancelled = False
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX))
    # Greift auch dann, wenn das Fenster mitten im Lauf geschlossen wird: der Worker
    # ist ein Daemon-Thread und wird beim Interpreter-Shutdown abgeschnitten, das
    # finally unten also uebersprungen. atexit laeuft davor noch.
    cleanup = functools.partial(shutil.rmtree, workdir, ignore_errors=True)
    atexit.register(cleanup)
    emit("log", text=f"Arbeitsordner: {workdir}")

    try:
        # Zuerst alles kopieren, damit das Handy danach abgesteckt werden kann.
        staged: list[tuple[Path, Path]] = []
        for index, source in enumerate(sources, start=1):
            if cancel.is_set():
                cancelled = True
                break
            emit("status", text=f"Kopiere {index}/{total}: {source.name}")
            try:
                staged.append((source, audio.stage_file(source, workdir)))
            except (audio.AudioError, OSError) as exc:
                failed.append((source.name, str(exc)))
                emit("log", text=f"✗ {source.name}: {exc}")

        if staged and not cancelled:
            emit("log", text=f"{len(staged)} Datei(en) kopiert.")
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
                wav = audio.to_wav(copy, copy.with_suffix(".16k.wav"), cancel=cancel)

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

    return {"written": written, "failed": failed, "cancelled": cancelled}
