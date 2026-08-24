"""GTK3-Oberfläche: Dateien wählen, Ordner einstellen, Transkription im Hintergrund starten."""

import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import config  # noqa: E402
from .audio import AUDIO_EXTENSIONS  # noqa: E402
from .job import JobError, run_job  # noqa: E402
from .transcriber import Transcriber  # noqa: E402

APP_ID = "org.audiotranskript.AudioTranskript"
TITLE = "Audio-Transkript"


def _apply_dark_preference() -> None:
    """Dunkles Design übernehmen, wenn der Desktop es vorgibt (Mint-Y-Dark greift ohnehin automatisch)."""
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    source = Gio.SettingsSchemaSource.get_default()
    if source is None:
        return
    for schema_id, key, dark_values in (
        ("org.gnome.desktop.interface", "color-scheme", ("prefer-dark",)),
        ("org.cinnamon.desktop.interface", "gtk-theme", None),
    ):
        schema = source.lookup(schema_id, True)
        if schema is None or not schema.has_key(key):
            continue
        value = Gio.Settings.new(schema_id).get_string(key)
        is_dark = value in dark_values if dark_values else "dark" in value.lower()
        if is_dark:
            settings.set_property("gtk-application-prefer-dark-theme", True)
            return


class Window(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=TITLE)
        self.cfg = config.load()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.transcriber = Transcriber()

        self.set_default_size(720, 540)
        self.set_size_request(600, 460)

        header = Gtk.HeaderBar(title=TITLE, show_close_button=True)
        self.header = header
        self.set_titlebar(header)
        open_button = Gtk.Button(label="Zielordner öffnen")
        open_button.connect("clicked", self.on_open_output)
        header.pack_end(open_button)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=14)
        self.add(outer)
        outer.pack_start(self._build_settings(), False, False, 0)
        outer.pack_start(self._build_actions(), False, False, 0)
        outer.pack_start(self._build_log(), True, True, 0)

        self.connect("delete-event", self.on_delete)
        self.refresh_start_tooltip()
        self.select_button.grab_focus()
        self.save_settings()

    # ---------- Aufbau ----------

    @staticmethod
    def _heading(text: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0.0)
        label.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        return label

    @staticmethod
    def _label(text: str) -> Gtk.Label:
        return Gtk.Label(label=text, xalign=0.0)

    def _path_row(self, grid: Gtk.Grid, row: int, text: str, value: str, handler):
        grid.attach(self._label(text), 0, row, 1, 1)
        entry = Gtk.Entry(text=value, hexpand=True)
        entry.set_editable(False)
        entry.set_tooltip_text(value)
        grid.attach(entry, 1, row, 1, 1)
        button = Gtk.Button(label="Ändern…")
        button.connect("clicked", handler)
        grid.attach(button, 2, row, 1, 1)
        return entry

    def _build_settings(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(self._heading("Einstellungen"), False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=10)
        box.pack_start(grid, False, False, 0)
        self.start_entry = self._path_row(
            grid, 0, "Dateiauswahl startet in:", self.cfg["start_dir"], self.on_choose_start_dir
        )
        self.auto_newest = Gtk.CheckButton(label="Automatisch in den neuesten Wochenordner (z. B. 202634)")
        self.auto_newest.set_active(self.cfg["auto_newest"])
        self.auto_newest.set_tooltip_text(
            "WhatsApp legt Sprachnachrichten in Wochenordnern ab.\n"
            "Abschalten, wenn der Dialog immer genau im oben gewählten Ordner öffnen soll."
        )
        self.auto_newest.connect("toggled", self.on_auto_newest_toggled)
        grid.attach(self.auto_newest, 1, 1, 2, 1)
        self.output_entry = self._path_row(
            grid, 2, "Transkripte speichern in:", self.cfg["output_dir"], self.on_choose_output_dir
        )

        options = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        grid.attach(options, 0, 3, 3, 1)
        options.pack_start(self._label("Modell:"), False, False, 0)
        self.model_box = Gtk.ComboBoxText()
        for name in config.MODELS:
            self.model_box.append_text(name)
        self.model_box.set_active(config.MODELS.index(self.cfg["model"]))
        self.model_box.connect("changed", lambda _w: self.save_settings())
        options.pack_start(self.model_box, False, False, 0)

        options.pack_start(self._label("Sprache:"), False, False, 8)
        self.language_box = Gtk.ComboBoxText()
        codes = [code for code, _label in config.LANGUAGES]
        for _code, label in config.LANGUAGES:
            self.language_box.append_text(label)
        self.language_box.set_active(codes.index(self.cfg["language"]))
        self.language_box.connect("changed", lambda _w: self.save_settings())
        options.pack_start(self.language_box, False, False, 0)

        self.timestamps = Gtk.CheckButton(label="Zeitmarken")
        self.timestamps.set_active(self.cfg["timestamps"])
        self.timestamps.connect("toggled", lambda _w: self.save_settings())
        options.pack_start(self.timestamps, False, False, 8)
        return box

    def _build_actions(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.pack_start(row, False, False, 0)

        self.select_button = Gtk.Button(label="Audiodateien auswählen…")
        self.select_button.get_style_context().add_class("suggested-action")
        self.select_button.set_size_request(-1, 42)
        self.select_button.connect("clicked", self.on_choose_files)
        row.pack_start(self.select_button, True, True, 0)

        self.cancel_button = Gtk.Button(label="Abbrechen")
        self.cancel_button.set_size_request(-1, 42)
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self.on_cancel)
        row.pack_start(self.cancel_button, False, False, 0)

        self.progress = Gtk.ProgressBar(show_text=False)
        box.pack_start(self.progress, False, False, 0)
        self.status = self._label("Bereit.")
        self.status.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        box.pack_start(self.status, False, False, 0)
        return box

    def _build_log(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(self._heading("Protokoll"), False, False, 0)
        scroller = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.log_view.set_left_margin(6)
        self.log_view.set_right_margin(6)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroller.add(self.log_view)
        box.pack_start(scroller, True, True, 0)
        return box

    # ---------- Einstellungen ----------

    def save_settings(self) -> None:
        codes = [code for code, _label in config.LANGUAGES]
        self.cfg = {
            "start_dir": self.start_entry.get_text(),
            "output_dir": self.output_entry.get_text(),
            "model": config.MODELS[self.model_box.get_active()],
            "language": codes[self.language_box.get_active()],
            "timestamps": self.timestamps.get_active(),
            "auto_newest": self.auto_newest.get_active(),
        }
        try:
            config.save(self.cfg)
        except OSError as exc:
            self.append_log(f"Einstellungen konnten nicht gespeichert werden: {exc}")
        self.cfg = config.load()

    def _choose_folder(self, title: str, entry: Gtk.Entry) -> None:
        dialog = Gtk.FileChooserDialog(
            title=title, transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_buttons("_Abbrechen", Gtk.ResponseType.CANCEL, "_Auswählen", Gtk.ResponseType.ACCEPT)
        dialog.set_current_folder(config.existing_dir(entry.get_text()))
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            chosen = dialog.get_filename()
            if chosen:
                entry.set_text(chosen)
                entry.set_tooltip_text(chosen)
                self.save_settings()
        dialog.destroy()

    def dialog_start_folder(self) -> str:
        pinned = self.start_entry.get_text()
        if self.auto_newest.get_active():
            return config.newest_week_dir(pinned)
        return config.existing_dir(pinned)

    def on_auto_newest_toggled(self, _button) -> None:
        self.save_settings()
        self.refresh_start_tooltip()

    def refresh_start_tooltip(self) -> None:
        pinned = self.start_entry.get_text()
        target = self.dialog_start_folder()
        text = pinned if target == pinned else f"{pinned}\nDialog öffnet in: {target}"
        self.start_entry.set_tooltip_text(text)

    def on_choose_start_dir(self, _button) -> None:
        self._choose_folder("Startordner für die Dateiauswahl", self.start_entry)
        self.refresh_start_tooltip()

    def on_choose_output_dir(self, _button) -> None:
        self._choose_folder("Zielordner für die Transkripte", self.output_entry)

    def on_open_output(self, _button) -> None:
        target = config.existing_dir(self.output_entry.get_text())
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(target).get_uri(), None)
        except GLib.Error as exc:
            self.message(Gtk.MessageType.ERROR, f"Ordner konnte nicht geöffnet werden:\n{exc.message}")

    # ---------- Ablauf ----------

    def on_choose_files(self, _button) -> None:
        if self.worker and self.worker.is_alive():
            return
        dialog = Gtk.FileChooserDialog(
            title="Audiodateien auswählen", transient_for=self, action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons("_Abbrechen", Gtk.ResponseType.CANCEL, "_Öffnen", Gtk.ResponseType.ACCEPT)
        dialog.set_select_multiple(True)
        dialog.set_current_folder(self.dialog_start_folder())

        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audiodateien")
        audio_filter.add_mime_type("audio/*")
        for extension in AUDIO_EXTENSIONS:
            audio_filter.add_pattern(f"*{extension}")
            audio_filter.add_pattern(f"*{extension.upper()}")
        dialog.add_filter(audio_filter)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("Alle Dateien")
        all_filter.add_pattern("*")
        dialog.add_filter(all_filter)

        files: list[str] = []
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            files = dialog.get_filenames()
        dialog.destroy()
        if files:
            self.start(files)

    def start(self, files: list[str]) -> None:
        self.cancel.clear()
        self.select_button.set_sensitive(False)
        self.cancel_button.set_sensitive(True)
        self.progress.set_fraction(0.0)
        self.progress.set_show_text(True)
        self.progress.set_text("0 %")
        self.set_status(f"{len(files)} Datei(en) ausgewählt.")
        self.append_log(f"--- Start: {len(files)} Datei(en) ---")

        cfg = dict(self.cfg)

        def emit(kind: str, **payload) -> None:
            GLib.idle_add(self.on_event, kind, payload)

        def work() -> None:
            try:
                emit("done", summary=run_job(files, cfg, emit, self.cancel, self.transcriber))
            except JobError as exc:
                emit("failed", message=str(exc))
            except Exception as exc:  # unerwartet – trotzdem sichtbar machen
                emit("failed", message=f"{type(exc).__name__}: {exc}")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def on_cancel(self, _button) -> None:
        self.cancel.set()
        self.cancel_button.set_sensitive(False)
        self.set_status("Abbruch angefordert – laufende Datei wird beendet…")

    def on_event(self, kind: str, payload: dict) -> bool:
        if kind == "status":
            self.set_status(payload["text"])
        elif kind == "log":
            self.append_log(payload["text"])
        elif kind == "progress":
            value = max(0.0, min(1.0, payload["value"] / 100.0))
            self.progress.set_fraction(value)
            self.progress.set_text(f"{value * 100:.0f} %")
        elif kind == "done":
            self.finish(payload["summary"])
        elif kind == "failed":
            self.finish(None, payload["message"])
        return False

    def finish(self, summary: dict | None, error: str | None = None) -> None:
        self.select_button.set_sensitive(True)
        self.cancel_button.set_sensitive(False)
        if error:
            self.progress.set_fraction(0.0)
            self.progress.set_show_text(False)
            self.set_status("Fehler.")
            self.append_log(f"✗ {error}")
            self.message(Gtk.MessageType.ERROR, error)
            return

        written, failed = summary["written"], summary["failed"]
        parts = [f"{len(written)} Transkript(e) erstellt"]
        if failed:
            parts.append(f"{len(failed)} fehlgeschlagen")
        if summary["cancelled"]:
            parts.append("abgebrochen")
        text = ", ".join(parts) + "."
        self.set_status(text)
        self.append_log(f"--- Fertig: {text} ---")

        if written and not failed:
            self.message(
                Gtk.MessageType.INFO,
                f"{len(written)} Transkript(e) gespeichert in:\n{self.output_entry.get_text()}",
            )
        elif failed:
            details = "\n".join(f"• {name}: {reason}" for name, reason in failed[:6])
            self.message(Gtk.MessageType.WARNING, f"{text}\n\n{details}")

    def message(self, kind: Gtk.MessageType, text: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=kind,
            buttons=Gtk.ButtonsType.OK, text=TITLE,
        )
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def set_status(self, text: str) -> None:
        self.status.set_text(text)
        self.status.set_tooltip_text(text)

    def append_log(self, text: str) -> None:
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text + "\n")
        self.log_view.scroll_to_mark(buffer.get_insert(), 0.0, False, 0.0, 0.0)

    def on_delete(self, _widget, _event) -> bool:
        if self.worker and self.worker.is_alive():
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO, text="Transkription läuft. Wirklich beenden?",
            )
            answer = dialog.run()
            dialog.destroy()
            if answer != Gtk.ResponseType.YES:
                return True
            self.cancel.set()
        return False


class Application(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            _apply_dark_preference()
            window = Window(self)
        window.show_all()
        window.present()


def main() -> int:
    GLib.set_prgname("audio-transkript")
    Gdk.set_program_class("Audio-Transkript")
    return Application().run(sys.argv[:1])
