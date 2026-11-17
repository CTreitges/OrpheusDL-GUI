"""Help and focus by keyboard (gui review: hover-only help, invisible focus).

Every "?" and every control with help is a Tab stop, shows a ring in
FOCUS_RING while focused, shows its help text on focus as on hover, and a
button is pressed with Enter or Space.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

customtkinter = pytest.importorskip("customtkinter", reason="customtkinter not installed")
tkinter = pytest.importorskip("tkinter", reason="tkinter not available")
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("no X display", allow_module_level=True)

from hires import help_widget  # noqa: E402
from hires.ui_style import FOCUS_RING  # noqa: E402


@pytest.fixture
def root():
    window = None
    for _ in range(3):
        try:
            window = customtkinter.CTk()
            break
        except tkinter.TclError:
            continue
    if window is None:
        pytest.skip("cannot open a window")
    window.geometry("400x300")
    yield window
    window.destroy()


def _focus(widget, handlers_owner=None):
    """Run the widget's own FocusIn handler (a background test window gets no
    real keyboard focus from Windows)."""
    owner = handlers_owner or widget
    owner._hires_focus_handlers[0]()


def _blur(widget):
    widget._hires_focus_handlers[1]()


def _wait_tip(root, timeout=2.0):
    tip = help_widget._TIPS[str(root)]
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if tip.window is not None and tip.window.winfo_viewable():
            return tip
        time.sleep(0.02)
    return tip


def test_keyboard_help_marker_is_a_tab_stop_and_shows_help_on_focus(root):
    label = customtkinter.CTkLabel(root, text="Quality")
    label.grid(row=0, column=0, padx=10)
    icon = help_widget.attach_help(label, "general.quality", "Download quality.")
    root.update()

    assert str(tkinter.Frame.cget(icon, "takefocus")) == "1"
    assert tkinter.Misc.bind(icon, "<FocusIn>"), "no focus handler"
    _focus(icon)
    tip = _wait_tip(root)
    assert tip.window.winfo_viewable(), "help did not appear on keyboard focus"
    assert "Recommended:" in tip.label.cget("text")
    assert icon.cget("fg_color") == FOCUS_RING, "no visible focus on the marker"

    _blur(icon)
    root.update()
    assert not tip.window.winfo_viewable()
    assert icon.cget("fg_color") != FOCUS_RING


def test_keyboard_help_button_ring_and_enter(root):
    pressed = []
    button = customtkinter.CTkButton(root, text="Port & queue", command=lambda: pressed.append(1),
                                     border_width=0)
    button.pack()
    help_widget.attach_tooltip(button, "hires.port.port_and_queue")
    root.update()

    assert str(tkinter.Frame.cget(button, "takefocus")) == "1"
    # The real app showed an invisible second stop: the button's inner canvas
    # (gui.py's bindings make Tk count it as focusable). Only the button counts.
    inner = button.winfo_children()
    assert inner and all(str(tkinter.Misc.cget(child, "takefocus")) == "0" for child in inner)
    for key in ("<Return>", "<space>", "<KP_Enter>"):
        assert tkinter.Misc.bind(button, key), f"{key} does not press the button"
    _focus(button)
    assert button.cget("border_color") == FOCUS_RING and button.cget("border_width") == 2
    button._hires_key_press()
    assert pressed == [1]
    _blur(button)
    assert button.cget("border_width") == 0


def test_keyboard_help_disabled_button_is_not_pressed(root):
    pressed = []
    button = customtkinter.CTkButton(root, text="Queue", command=lambda: pressed.append(1), state="disabled")
    button.pack()
    help_widget.attach_tooltip(button, "hires.port.queue_matches")
    button._hires_key_press()
    assert pressed == []


def test_keyboard_help_entry_keeps_its_own_focus(root):
    entry = customtkinter.CTkEntry(root)
    entry.pack()
    help_widget.attach_tooltip(entry, "hires.port.url")
    root.update()
    assert tkinter.Misc.bind(entry._entry, "<FocusIn>"), "the entry's help does not follow focus"
    _focus(entry)
    assert entry.cget("border_color") == FOCUS_RING


def test_keyboard_help_every_hires_control_is_reachable(root, tmp_path):
    """Through the real assembly: each control with help is a Tab stop."""
    from hires.integration import setup_tabs
    from test_integration import FakeGui

    gui = FakeGui(output_path=str(tmp_path))
    gui.app = root
    gui.application_path = str(tmp_path)
    view = customtkinter.CTkTabview(master=root)
    view.pack(fill="both", expand=True)
    result = setup_tabs(gui, view, queue_path=str(tmp_path / "q.json"))
    assert result is not None
    root.update()

    pending, missing, count = [root], [], 0
    while pending:
        widget = pending.pop()
        pending.extend(widget.winfo_children())
        if getattr(widget, "_hires_help_key", None):
            count += 1
            if not getattr(widget, "_hires_keyboard", False):
                missing.append(widget._hires_help_key)
    assert count > 20, "found suspiciously few controls with help"
    assert missing == [], f"not reachable by keyboard: {sorted(missing)}"

    port = result["tabs"]["port"]
    view.set("Port")  # Tab skips what is not on screen
    root.update()
    # Link field -> "Port" -> "Port & queue": no invisible stop in between
    # (customtkinter's inner canvas of a button was one, without ring or help).
    first = port.url_entry._entry.tk_focusNext()
    second = first.tk_focusNext()
    assert second is port.port_queue_button, f"second Tab from the link field lands on {second}"
