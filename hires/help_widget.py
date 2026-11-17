"""The "?" help marker and the visible recommendation hint.

One building block for every place that needs an explanation: a small "?"
next to a setting's label that shows :func:`hires.help_texts.help_message` on
hover. Controls of the hi-res tabs get the same text as a tooltip on the
control itself, which keeps the toolbars free of a "?" per button.

Every widget that receives help is tagged with ``_hires_help_key``, which is
how the coverage test proves the texts are actually attached and not just
written down.
"""

from __future__ import annotations

import tkinter
from typing import Optional

import customtkinter

from .help_texts import help_message, short_hint
from .ui_style import FOCUS_RING, FOCUS_TEXT, GRAY_TEXT, SECONDARY_TEXT, WHITE_TEXT

#: Tooltip background, the same as gui.py's TOOLTIP_MENU_BG.
TOOLTIP_BG = "#222323"

#: Room kept free to the right of a label for its "?".
ICON_ROOM = 22


class _SharedTip:
    """One tooltip window per main window, shared by every help marker.

    CTkToolTip builds a hidden Toplevel per widget. With a "?" on ~90
    settings and tooltips on every queue header that is a hundred-odd
    windows, and creating them showed up as a large part of the queue's
    first paint. One window, re-texted on hover, looks the same and costs
    nothing per widget.
    """

    DELAY_MS = 350

    def __init__(self, toplevel):
        self.toplevel = toplevel
        self.window = None
        self.label = None
        self._after = None

    def _ensure(self):
        if self.window is not None and self.window.winfo_exists():
            return
        self.window = tkinter.Toplevel(self.toplevel)
        self.window.withdraw()
        self.window.overrideredirect(True)
        try:
            self.window.attributes("-topmost", True)
        except Exception:
            pass
        family = customtkinter.CTkFont().cget("family")
        try:
            scale = customtkinter.ScalingTracker.get_widget_scaling(self.toplevel)
        except Exception:
            scale = 1.0
        self.label = tkinter.Label(
            self.window,
            text="",
            justify="left",
            wraplength=int(460 * scale),
            bg=TOOLTIP_BG,
            fg=WHITE_TEXT,
            padx=int(12 * scale),
            pady=int(10 * scale),
            font=(family, -round(12 * scale)),
            highlightthickness=1,
            highlightbackground="#3A3C40",
        )
        self.label.pack()

    def schedule(self, widget, message: str) -> None:
        self.cancel()
        self._after = self.toplevel.after(self.DELAY_MS, lambda: self._show(widget, message))

    def _show(self, widget, message: str) -> None:
        self._after = None
        try:
            if not widget.winfo_exists():
                return
            self._ensure()
            self.label.configure(text=message)
            x, y = widget.winfo_pointerx(), widget.winfo_pointery()
            left, top = widget.winfo_rootx(), widget.winfo_rooty()
            inside = left <= x <= left + widget.winfo_width() and top <= y <= top + widget.winfo_height()
            if inside:
                x, y = x + 16, y + 14
            else:
                # Opened without the mouse on it (keyboard focus, a test):
                # anchor to the widget instead of wherever the pointer is.
                x, y = left, top + widget.winfo_height() + 6
            self.window.geometry(f"+{x}+{y}")
            self.window.deiconify()
            self.window.lift()
        except Exception:
            pass

    def cancel(self) -> None:
        if self._after is not None:
            try:
                self.toplevel.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self.window is not None:
            try:
                self.window.withdraw()
            except Exception:
                pass


_TIPS: dict = {}


def _tip_for(widget) -> _SharedTip:
    toplevel = widget.winfo_toplevel()
    tip = _TIPS.get(str(toplevel))
    if tip is None or tip.toplevel is not toplevel:
        tip = _SharedTip(toplevel)
        _TIPS[str(toplevel)] = tip
    return tip


def _tooltip(widget, message: str) -> None:
    tip = _tip_for(widget)
    widget._hires_help_text = message
    widget.bind("<Enter>", lambda _e: tip.schedule(widget, message), add="+")
    widget.bind("<Leave>", lambda _e: tip.cancel(), add="+")
    widget.bind("<ButtonPress>", lambda _e: tip.cancel(), add="+")
    keyboard_reachable(widget, message)


# -- keyboard -------------------------------------------------------------------
#
# customtkinter draws its controls on canvases that Tab skips and that show no
# focus. Every control with help (and every "?") is made a Tab stop: a ring in
# FOCUS_RING shows where the focus is, the help text appears as on hover, and
# Enter or Space presses a button.

def _ring(widget, on: bool) -> None:
    """Draw or remove the focus ring: the border where the control has one."""
    saved = getattr(widget, "_hires_ring_saved", None)
    try:
        if on:
            if saved is None:
                # Buttons and entries draw a border to show the ring with; a
                # label ("?") draws none, so it is filled instead.
                if not isinstance(widget, customtkinter.CTkLabel):
                    saved = ("border", widget.cget("border_width"), widget.cget("border_color"))
                else:
                    saved = ("fill", widget.cget("fg_color"), widget.cget("text_color"))
                widget._hires_ring_saved = saved
            if saved[0] == "border":
                widget.configure(border_width=2, border_color=FOCUS_RING)
            else:
                widget.configure(fg_color=FOCUS_RING, text_color=FOCUS_TEXT)
        elif saved is not None:
            if saved[0] == "border":
                widget.configure(border_width=saved[1], border_color=saved[2])
            else:
                widget.configure(fg_color=saved[1], text_color=saved[2])
    except Exception:
        pass  # widget gone, or a kind without either option


def keyboard_reachable(widget, message: str = "") -> None:
    """Make ``widget`` a Tab stop with a visible ring and its help on focus."""
    try:
        entry = getattr(widget, "_entry", None)  # CTkEntry: its tk Entry takes the focus
        target = entry if isinstance(entry, tkinter.Entry) else widget
        if target is widget:
            tkinter.Frame.configure(widget, takefocus=1)
        tip = _tip_for(widget)

        def focus_in(_event=None):
            _ring(widget, True)
            if message:
                tip.schedule(widget, message)

        def focus_out(_event=None):
            _ring(widget, False)
            tip.cancel()

        tkinter.Misc.bind(target, "<FocusIn>", focus_in, add="+")
        tkinter.Misc.bind(target, "<FocusOut>", focus_out, add="+")
        widget._hires_focus_handlers = (focus_in, focus_out)

        press = getattr(widget, "invoke", None) or getattr(widget, "_open_dropdown_menu", None)
        if target is widget and callable(press):
            def activate(_event=None):
                press()
                return "break"

            for key in ("<Return>", "<KP_Enter>", "<space>"):
                tkinter.Misc.bind(widget, key, activate, add="+")
            widget._hires_key_press = activate
        widget._hires_keyboard = True
    except Exception:
        pass


def help_icon(parent, key: str, base_text: str = ""):
    """A "?" label with the help text for ``key`` on hover. Caller places it."""
    icon = customtkinter.CTkLabel(
        parent,
        text="?",
        width=16,
        height=16,
        corner_radius=8,
        fg_color="#3A3C40",
        text_color=SECONDARY_TEXT,
        font=("", 11, "bold"),
        cursor="question_arrow",
    )
    icon._hires_help_key = key
    _tooltip(icon, help_message(key, base_text))
    return icon


def attach_help(label, key: str, base_text: str = ""):
    """Put a "?" right behind ``label`` without disturbing its layout.

    The icon is placed relative to the label (``place(in_=label)``), so it
    follows the label wherever the surrounding grid or pack puts it, and
    the label's right padding is widened so the icon never covers the field
    next to it. Returns the icon, or ``None`` when the label is gone.
    """
    if not help_message(key, base_text):
        return None  # a "?" with nothing behind it helps nobody
    try:
        master = label.master
        icon = help_icon(master, key, base_text)
        _widen_right_padding(label)
        icon.place(in_=label, relx=1.0, x=4, rely=0.5, anchor="w")
        return icon
    except Exception:
        return None


def _widen_right_padding(label) -> None:
    """Re-run the label's last grid/pack call with ``ICON_ROOM`` more padding.

    CTk widgets scale geometry arguments themselves and remember the unscaled
    call, so it is repeated through the widget's own method; going through
    ``grid_info`` would feed already-scaled numbers back in and scale twice.
    """
    last = getattr(label, "_last_geometry_manager_call", None)
    if not last:
        return
    kwargs = dict(last.get("kwargs") or {})
    left, right = _pad_pair(kwargs.get("padx", 0))
    kwargs["padx"] = (left, right + ICON_ROOM)
    manager = label.winfo_manager()
    if manager == "grid":
        label.grid(**kwargs)
    elif manager == "pack":
        label.pack(**kwargs)


def pack_help(parent, key: str, base_text: str = "", **pack_kwargs):
    """A "?" packed into a row of checkboxes (TIDAL's option row)."""
    if not help_message(key, base_text):
        return None
    try:
        icon = help_icon(parent, key, base_text)
        pack_kwargs.setdefault("side", "left")
        pack_kwargs.setdefault("padx", (2, 14))
        icon.pack(**pack_kwargs)
        return icon
    except Exception:
        return None


def attach_tooltip(widget, key: str):
    """Hover help on a control itself -- for buttons of the hi-res tabs."""
    try:
        widget._hires_help_key = key
        segments = getattr(widget, "_buttons_dict", None)
        if isinstance(segments, dict):
            # CTkSegmentedButton refuses bind(); its segments are buttons
            # that take hover, focus and Enter one by one.
            for segment in segments.values():
                _tooltip(segment, help_message(key))
            widget._hires_keyboard = True
        else:
            _tooltip(widget, help_message(key))
    except Exception:
        pass
    return widget


def recommendation_label(parent, key: str, wraplength: int = 420):
    """Small grey text with the visible recommendation, or ``None``."""
    text = short_hint(key)
    if not text:
        return None
    label = customtkinter.CTkLabel(
        parent,
        text=text,
        text_color=GRAY_TEXT,
        font=("", 11),
        anchor="w",
        justify="left",
        wraplength=wraplength,
    )
    label._hires_hint_key = key
    return label


def grid_recommendation(parent, key: str, row: int, column: int = 1, **grid_kwargs) -> bool:
    """Grid the recommendation for ``key`` at ``row``. True when one was placed."""
    label = recommendation_label(parent, key, wraplength=grid_kwargs.pop("wraplength", 420))
    if label is None:
        return False
    grid_kwargs.setdefault("sticky", "w")
    grid_kwargs.setdefault("padx", (8, 5))
    grid_kwargs.setdefault("pady", (0, 4))
    label.grid(row=row, column=column, **grid_kwargs)
    return True


def _pad_pair(value) -> tuple:
    """``padx`` from ``grid_info``/``pack_info`` as ``(left, right)`` ints."""
    if isinstance(value, (tuple, list)):
        if len(value) >= 2:
            return int(value[0]), int(value[1])
        if value:
            return int(value[0]), int(value[0])
        return 0, 0
    try:
        parts = str(value).split()
    except Exception:
        return 0, 0
    if len(parts) >= 2:
        return int(float(parts[0])), int(float(parts[1]))
    if parts:
        number = int(float(parts[0]))
        return number, number
    return 0, 0


def help_keys_in(widget) -> set:
    """Every help key attached somewhere below ``widget`` (for the tests)."""
    found = set()
    pending = [widget]
    while pending:
        current = pending.pop()
        key: Optional[str] = getattr(current, "_hires_help_key", None)
        if key:
            found.add(key)
        try:
            pending.extend(current.winfo_children())
        except Exception:
            continue
    return found
