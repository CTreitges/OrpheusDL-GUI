"""The update dialog -- widgets only; every decision is in update_flow.py.

One small window that changes its content with the update's state: offer
(version, changes, "Update now"), download progress, waiting for a running
download, installing, failure or a plain message. Escape and the window's X
mean "Later" -- or Cancel while a download runs or the update waits.
"""

from __future__ import annotations

import customtkinter

from .app_version import short_label
from .ui_style import (
    BUTTON_BG,
    CONTAINER,
    ERROR,
    PRIMARY_ACTION,
    SECONDARY_TEXT,
    WHITE_TEXT,
    button,
)


def _megabytes(count: int) -> str:
    return f"{count / 1_000_000:.1f}"


def add_check_button(parent, updater, **pack_kwargs):
    """*About → Check for updates*, with its help tooltip. Returns the button."""
    from .help_widget import attach_tooltip

    widget = button(parent, "Check for updates", updater.check_now, width=150)
    widget.pack(**(pack_kwargs or {"pady": (0, 10)}))
    attach_tooltip(widget, "hires.about.check_for_updates")
    return widget


class UpdateDialog:
    def __init__(self, parent, controller):
        self.controller = controller
        self.window = customtkinter.CTkToplevel(parent)
        self.window.title("OrpheusDL update")
        self.window.resizable(False, False)
        try:
            self.window.transient(parent.winfo_toplevel())
        except Exception:
            pass
        self.window.protocol("WM_DELETE_WINDOW", self._closed_by_user)
        self.window.bind("<Escape>", lambda _e: self._closed_by_user())

        self.title = customtkinter.CTkLabel(
            self.window, text="", text_color=WHITE_TEXT, font=("Segoe UI", 15, "bold"), anchor="w"
        )
        self.title.pack(fill="x", padx=20, pady=(16, 4))
        self.message = customtkinter.CTkLabel(
            self.window, text="", text_color=SECONDARY_TEXT, wraplength=440, justify="left", anchor="w"
        )
        self.message.pack(fill="x", padx=20, pady=(0, 8))
        self.notes = customtkinter.CTkTextbox(self.window, height=140, width=440, fg_color=CONTAINER, wrap="word")
        self.progress_bar = customtkinter.CTkProgressBar(self.window, width=440)
        self.progress_bar.set(0)
        self.buttons = customtkinter.CTkFrame(self.window, fg_color="transparent")
        self.buttons.pack(side="bottom", fill="x", padx=20, pady=(8, 16))

    # -- states -----------------------------------------------------------------
    def offer(self, release, can_install: bool) -> None:
        current = short_label(self.controller.version)
        self.title.configure(text=f"OrpheusDL {short_label(release.tag)} is available")
        if can_install:
            text = (
                f"You have {current}. Update now downloads it from GitHub, checks it, "
                "replaces the program and restarts. Settings, logins, queue and "
                "downloads stay as they are; the old program is kept as "
                "OrpheusDL_GUI.old.exe."
            )
        else:
            text = (
                f"You have {current}. This copy cannot update itself -- download the "
                "new version from the release page."
            )
        self.message.configure(text=text, text_color=SECONDARY_TEXT)
        self._show_notes(release.notes)
        if can_install:
            self._buttons(
                ("Update now", self.controller.accept, PRIMARY_ACTION),
                ("Later", self._closed_by_user, BUTTON_BG),
                ("Release page", self.controller.open_release_page, BUTTON_BG),
            )
        else:
            self._buttons(
                ("Open release page", self.controller.open_release_page, PRIMARY_ACTION),
                ("Later", self._closed_by_user, BUTTON_BG),
            )

    def progress(self, done: int, total: int) -> None:
        self.notes.pack_forget()
        if not self.progress_bar.winfo_ismapped():
            self.progress_bar.pack(fill="x", padx=20, pady=(0, 6), before=self.buttons)
            self._buttons(("Cancel", self.controller.cancel, BUTTON_BG), enter=False)
        self.title.configure(text="Downloading the update")
        self.progress_bar.set(done / total if total else 0)
        self.message.configure(text=f"{_megabytes(done)} of {_megabytes(total)} MB")

    def waiting(self) -> None:
        self.notes.pack_forget()
        self.progress_bar.pack_forget()
        self.title.configure(text="Waiting for the download to finish")
        self.message.configure(
            text=(
                "The update is downloaded and checked. It installs and restarts "
                "OrpheusDL as soon as nothing downloads any more; the queue starts "
                "no new item until then. Cancel lets the queue go on."
            )
        )
        self._buttons(("Cancel update", self.controller.cancel, BUTTON_BG), enter=False)

    def installing(self) -> None:
        self.notes.pack_forget()
        self.progress_bar.pack_forget()
        self.title.configure(text="Installing")
        self.message.configure(text="OrpheusDL restarts in a moment.")
        self._buttons()

    def failed(self, message: str) -> None:
        self.notes.pack_forget()
        self.progress_bar.pack_forget()
        self.title.configure(text="Update not installed")
        self.message.configure(text=message, text_color=ERROR)
        self._buttons(("Close", self._closed_by_user, BUTTON_BG))

    def info(self, message: str) -> None:
        self.notes.pack_forget()
        self.progress_bar.pack_forget()
        self.title.configure(text="Check for updates")
        self.message.configure(text=message, text_color=SECONDARY_TEXT)
        self._buttons(("OK", self._closed_by_user, PRIMARY_ACTION))

    def close(self) -> None:
        try:
            self.window.destroy()
        except Exception:
            pass

    # -- helpers ----------------------------------------------------------------
    def _show_notes(self, notes: str) -> None:
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", (notes or "No release notes.").strip()[:1500])
        self.notes.configure(state="disabled")
        self.notes.pack(fill="x", padx=20, pady=(0, 6), before=self.buttons)

    def _buttons(self, *specs, enter: bool = True) -> None:
        """Replace the button row. Enter runs the first button when ``enter``.

        Not while a download runs or the update waits: there the first button
        cancels, and a stray Enter must not throw the download away.
        """
        for child in self.buttons.winfo_children():
            child.destroy()
        self.button_widgets = {}
        first = None
        for text, command, color in specs:
            widget = button(self.buttons, text, command, width=130, fg=color)
            widget.pack(side="left", padx=(0, 8))
            self.button_widgets[text] = widget
            if first is None:
                first = (widget, command)
        self.window.unbind("<Return>")
        self.enter_action = first[1] if (first is not None and enter) else None
        if first is not None:
            self.window.after(30, first[0].focus_set)
        if self.enter_action is not None:
            self.window.bind("<Return>", lambda _e: self.press_enter())

    def press_enter(self) -> None:
        """What Enter does in the current state (nothing while it would cancel)."""
        if self.enter_action is not None:
            self.enter_action()

    def _closed_by_user(self) -> None:
        if self.controller.state in (self.controller.DOWNLOADING, self.controller.WAITING):
            self.controller.cancel()
            return
        if self.controller.state == self.controller.OFFERED:
            self.controller.cancel()
            return
        self.controller.view_closed()
        self.close()
