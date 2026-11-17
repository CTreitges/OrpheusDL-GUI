"""Widgets of the "Convert folder…" dialog (Settings > Codecs > Target format).

Pick a folder, a format and whether to keep the originals; the dialog counts
the audio files, asks before it replaces anything, then shows progress with a
Cancel button. The work itself is :class:`hires.convert_job.FolderConvertJob`.
"""

from __future__ import annotations

import tkinter
from tkinter import filedialog
from typing import Any, Callable, Optional

import customtkinter

from . import audio_convert as ac
from .controllers import UiDispatcher
from .convert_job import FolderConvertJob, Progress, summary_text
from .dialogs import confirm_destructive
from .download_finish import target_settings
from .help_widget import attach_tooltip
from .ui_style import PRIMARY_ACTION, SECONDARY_TEXT, WARNING, button


class ConvertFolderDialog:
    def __init__(self, parent, *, ffmpeg: str = "ffmpeg", target: str = "flac",
                 keep_original: bool = False, folder: str = "",
                 busy: Optional[Callable[[], bool]] = None):
        """``busy`` says whether a download runs right now; converting a folder
        then could catch a file that is still being written."""
        self.window = customtkinter.CTkToplevel(parent)
        self.window.title("Convert folder")
        self.window.resizable(False, False)
        self.window.transient(parent.winfo_toplevel())
        self.dispatch = UiDispatcher(self.window)
        self.job = FolderConvertJob(self.dispatch, ffmpeg=ffmpeg)
        self.busy = busy or (lambda: False)
        self._paths: list = []
        self._confirm = confirm_destructive

        body = customtkinter.CTkFrame(self.window, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.grid_columnconfigure(1, weight=1)

        customtkinter.CTkLabel(body, text="Folder").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.folder_var = tkinter.StringVar(value=folder)
        self.folder_entry = customtkinter.CTkEntry(body, textvariable=self.folder_var, width=360)
        self.folder_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.browse_button = button(body, "Browse…", self.browse, width=90)
        self.browse_button.grid(row=0, column=2, padx=(8, 0), pady=4)

        customtkinter.CTkLabel(body, text="Format").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.format_var = tkinter.StringVar(value=target if target in ac.TARGET_FORMATS else "flac")
        self.format_box = customtkinter.CTkComboBox(body, variable=self.format_var, values=list(ac.TARGET_FORMATS),
                                                    state="readonly", width=120)
        self.format_box.grid(row=1, column=1, sticky="w", pady=4)

        self.keep_var = tkinter.BooleanVar(value=keep_original)
        self.keep_box = customtkinter.CTkCheckBox(body, text="Keep original", variable=self.keep_var)
        self.keep_box.grid(row=2, column=1, sticky="w", pady=4)

        self.progress = customtkinter.CTkProgressBar(body, width=360)
        self.progress.set(0)
        self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        self.status = customtkinter.CTkLabel(body, text="Pick a folder.", text_color=SECONDARY_TEXT,
                                             wraplength=460, justify="left", anchor="w")
        self.status.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        actions = customtkinter.CTkFrame(body, fg_color="transparent")
        actions.grid(row=5, column=0, columnspan=3, sticky="e")
        self.cancel_button = button(actions, "Close", self.cancel_or_close, width=100)
        self.cancel_button.pack(side="right")
        self.start_button = button(actions, "Convert", self.start, width=110, fg=PRIMARY_ACTION)
        self.start_button.pack(side="right", padx=(0, 8))
        attach_tooltip(self.start_button, "hires.convert.folder")
        attach_tooltip(self.cancel_button, "hires.convert.cancel")

        self.window.bind("<Escape>", lambda _e: self.cancel_or_close())
        self.window.protocol("WM_DELETE_WINDOW", self.cancel_or_close)

    # -- actions ----------------------------------------------------------
    def browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self.window, title="Folder to convert",
                                         initialdir=self.folder_var.get() or None)
        if chosen:
            self.folder_var.set(chosen)

    def start(self, confirm=None) -> bool:
        """Count the files (off the main thread), ask, then convert."""
        if self.job.running:
            return False
        if self.busy():
            self.status.configure(
                text="A download is running. Wait until it has finished (or pause the queue): "
                     "converting now could catch a file that is still being written.",
                text_color=WARNING)
            return False
        self._confirm = confirm or confirm_destructive
        self._set_running(True)
        self.progress.set(0)
        self.status.configure(text="Counting audio files…", text_color=SECONDARY_TEXT)
        return self.job.scan(self.folder_var.get().strip(), self._on_scanned)

    def _on_scanned(self, audio: list, playlists: list) -> None:
        self._set_running(False)
        self._paths = audio
        target = self.format_var.get()
        if not audio:
            self.status.configure(text="No audio files in this folder.", text_color=WARNING)
            return
        keep = bool(self.keep_var.get())
        if not keep and not self._confirm(
            self.window, "Replace originals?",
            f"{len(audio)} audio files will be converted to {target.upper()}. Each original is "
            "deleted once its converted copy checked out, with all its tags. Files that fail, and "
            "files with tags the new format cannot hold, stay as they are.",
            "Convert and replace",
        ):
            self.status.configure(text="Nothing changed.", text_color=SECONDARY_TEXT)
            return
        self._set_running(True)
        self.status.configure(text=f"Converting {len(audio)} files…", text_color=SECONDARY_TEXT)
        self.job.start(audio, target, keep, self._on_progress, self._on_done, playlists=playlists)

    def cancel_or_close(self) -> None:
        if self.job.running:
            self.job.cancel()
            self.status.configure(text="Stopping… the file being converted keeps its original.")
            return
        self.dispatch.stop()
        self.window.destroy()

    # -- job callbacks (main thread) --------------------------------------
    def _on_progress(self, progress: Progress) -> None:
        if progress.total:
            self.progress.set(progress.done / progress.total)
        self.status.configure(text=f"{progress.text}\n{progress.current}")

    def _on_done(self, summary: ac.FolderSummary) -> None:
        self._set_running(False)
        self.status.configure(text=summary_text(summary, self.format_var.get()),
                              text_color=WARNING if summary.failed else SECONDARY_TEXT)

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in (self.start_button, self.browse_button, self.folder_entry, self.keep_box):
            widget.configure(state=state)
        self.format_box.configure(state="disabled" if running else "readonly")
        self.cancel_button.configure(text="Cancel" if running else "Close")


def open_convert_dialog(gui: Any) -> Optional[ConvertFolderDialog]:
    """Open the dialog from gui.py, preset from the current settings."""
    app = getattr(gui, "app", None)
    if app is None:
        return None
    settings = getattr(gui, "current_settings", None)
    target, keep = target_settings(settings)
    general = (settings or {}).get("globals", {}).get("general", {}) if isinstance(settings, dict) else {}
    finder = getattr(gui, "_get_ffmpeg_path", None)
    ffmpeg = (finder() if callable(finder) else None) or "ffmpeg"
    bundle = getattr(gui, "hires_runtime", None)
    runtime = bundle.get("runtime") if isinstance(bundle, dict) else None
    busy = runtime.is_busy if runtime is not None else (
        lambda: bool(getattr(gui, "download_process_active", False)))
    return ConvertFolderDialog(app, ffmpeg=ffmpeg, target=target if target != ac.TARGET_OFF else "flac",
                               keep_original=keep, folder=general.get("output_path") or "", busy=busy)
