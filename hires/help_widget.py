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

from typing import Optional

import customtkinter

from .help_texts import help_message, short_hint
from .ui_style import GRAY_TEXT, SECONDARY_TEXT, WHITE_TEXT

#: Tooltip background, the same as gui.py's TOOLTIP_MENU_BG.
TOOLTIP_BG = "#222323"

#: Room kept free to the right of a label for its "?".
ICON_ROOM = 22


def _tooltip(widget, message: str) -> None:
    from CTkToolTip import CTkToolTip

    CTkToolTip(
        widget,
        message=message,
        bg_color=TOOLTIP_BG,
        text_color=WHITE_TEXT,
        padx=12,
        pady=12,
        justify="left",
        wraplength=460,
    )


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
