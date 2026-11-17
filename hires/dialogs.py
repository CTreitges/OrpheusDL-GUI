"""A confirmation dialog for actions that cannot be undone.

The stock GUI's yes/no box focuses "Yes" and answers it on Enter. For "Clear
all" that is the wrong default: a stray Enter would wipe the queue. This one
names the action on its button, puts the focus on Cancel and treats Escape and
closing the window as Cancel.
"""

from __future__ import annotations

import customtkinter

from .ui_style import BUTTON_BG, DANGER_BG, DANGER_HOVER, SECONDARY_TEXT, button


class ConfirmDialog:
    """Modal yes/no with a named destructive button. Read ``result`` after."""

    def __init__(self, parent, title: str, message: str, confirm_label: str):
        self.result = False
        self.window = customtkinter.CTkToplevel(parent)
        self.window.title(title)
        self.window.resizable(False, False)
        self.window.transient(parent.winfo_toplevel())

        customtkinter.CTkLabel(
            self.window,
            text=message,
            text_color=SECONDARY_TEXT,
            wraplength=420,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 14))

        row = customtkinter.CTkFrame(self.window, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        self.cancel_button = button(row, "Cancel", self.cancel, width=110, fg=BUTTON_BG)
        self.cancel_button.pack(side="right")
        self.confirm_button = button(
            row, confirm_label, self.confirm, width=130, fg=DANGER_BG, hover_color=DANGER_HOVER
        )
        self.confirm_button.pack(side="right", padx=(0, 8))

        self.window.bind("<Escape>", lambda _e: self.cancel())
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.after(50, self._focus)

    def _focus(self) -> None:
        try:
            self.cancel_button.focus_set()
            self.window.grab_set()
        except Exception:
            pass  # window already closed, or not viewable yet for the grab

    def confirm(self) -> None:
        self.result = True
        self.window.destroy()

    def cancel(self) -> None:
        self.result = False
        self.window.destroy()


def confirm_destructive(parent, title: str, message: str, confirm_label: str) -> bool:
    """Show :class:`ConfirmDialog` and block until it is answered."""
    dialog = ConfirmDialog(parent, title, message, confirm_label)
    parent.wait_window(dialog.window)
    return dialog.result
