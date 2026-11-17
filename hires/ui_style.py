"""Palette and the small widget helpers every hi-res tab shares.

Mirrors the palette in gui.py. Duplicated rather than imported because gui.py
is the __main__ module and importing it back would be circular.
"""

from __future__ import annotations

import customtkinter

WHITE_TEXT = "#E4E5E6"
SECONDARY_TEXT = "#A8A8AA"
GRAY_TEXT = "#9A9C9F"  # 4.5:1 on CONTAINER; #8A8C8F was 4.2:1
SURFACE = "#1D1E1E"
CONTAINER = "#2B2B2B"
BORDER = "#565B5E"
HOVER = "#474747"
BUTTON_BG = "#35373b"
PRIMARY_ACTION = "#2D5A88"
SUCCESS = "#00C851"
WARNING = "#F2C94C"
ERROR = "#E53935"
DANGER_BG = "#6B2B2B"
DANGER_HOVER = "#8A3535"
ROW_HOVER = "#272828"
#: Keyboard focus ring of the hi-res controls and "?" markers (WCAG 2.4.7);
#: at least 3:1 against every background it is drawn on (tests/test_ui_contrast.py).
FOCUS_RING = "#6CB4FF"
#: Text on a "?" marker filled with FOCUS_RING while it has the focus.
FOCUS_TEXT = "#101214"


def button(parent, text, command, width=110, fg=BUTTON_BG, **kwargs):
    kwargs.setdefault("hover_color", HOVER)
    return customtkinter.CTkButton(
        parent,
        text=text,
        command=command,
        width=width,
        height=28,
        fg_color=fg,
        **kwargs,
    )
