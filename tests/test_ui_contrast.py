"""Text of the hi-res tabs stays readable: WCAG 1.4.3 contrast >= 4.5:1.

Checks every text color of ``hires/ui_style.py`` on every background the
tabs draw text on. The GUI review of the DJ upgrade found grey detail text at
4.2:1 on the container background.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ui_style = pytest.importorskip("hires.ui_style", reason="customtkinter not installed")

TEXTS = ("WHITE_TEXT", "SECONDARY_TEXT", "GRAY_TEXT")
BACKGROUNDS = ("SURFACE", "CONTAINER", "ROW_HOVER")


def _luminance(hex_color):
    channels = [int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_formula_matches_known_values():
    assert round(contrast("#FFFFFF", "#000000"), 1) == 21.0
    assert round(contrast("#777777", "#FFFFFF"), 2) == 4.48


@pytest.mark.parametrize("text", TEXTS)
@pytest.mark.parametrize("background", BACKGROUNDS)
def test_text_colors_reach_4_5_to_1(text, background):
    ratio = contrast(getattr(ui_style, text), getattr(ui_style, background))
    assert ratio >= 4.5, f"{text} on {background}: {ratio:.2f}:1"
