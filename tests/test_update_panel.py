"""The update dialog with real widgets, driven by the real controller."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

customtkinter = pytest.importorskip("customtkinter", reason="customtkinter not installed")
tkinter = pytest.importorskip("tkinter", reason="tkinter not available")
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("no X display", allow_module_level=True)

from test_update_flow import RELEASE, Harness  # noqa: E402

from hires.update_panel import UpdateDialog, add_check_button  # noqa: E402


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
    window.geometry("200x100")
    yield window
    window.destroy()


def _harness(root, tmp_path, **kw):
    h = Harness(tmp_path, **kw)
    h.dialogs = []

    def factory():
        dialog = UpdateDialog(root, h.controller)
        h.dialogs.append(dialog)
        return dialog

    h.controller._view_factory = factory
    return h


def _press(dialog, key):
    dialog.window.event_generate(f"<{key}>")
    dialog.window.update()


def test_offer_shows_version_notes_and_update_now(root, tmp_path):
    h = _harness(root, tmp_path, busy=True)
    h.controller.check_now()
    dialog = h.dialogs[0]
    root.update()
    assert "hires.13 is available" in dialog.title.cget("text")
    assert "hires.12" in dialog.message.cget("text")
    assert list(dialog.button_widgets) == ["Update now", "Later", "Release page"]
    assert dialog.notes.get("1.0", "end").strip() == "Fixes"


def test_enter_accepts_and_waiting_ignores_enter(root, tmp_path):
    """Keyboard: Enter = Update now; while waiting Enter must not cancel."""
    h = _harness(root, tmp_path, busy=True)
    h.controller.check_now()
    dialog = h.dialogs[0]
    assert dialog.window.bind("<Return>"), "no Enter binding on the offer"
    dialog.press_enter()
    root.update()
    assert h.controller.state == h.controller.WAITING
    assert "Waiting" in dialog.title.cget("text")
    assert list(dialog.button_widgets) == ["Cancel update"]
    assert not dialog.window.bind("<Return>"), "Enter would cancel the waiting update"
    dialog.press_enter()
    assert h.controller.state == h.controller.WAITING


def test_escape_is_later(root, tmp_path):
    h = _harness(root, tmp_path)
    h.controller.check_now()
    dialog = h.dialogs[0]
    dialog._closed_by_user()
    assert h.controller.state == h.controller.IDLE
    assert h.controller.view is None


def test_failure_is_shown_in_red(root, tmp_path):
    h = _harness(root, tmp_path, stage_error="checksum mismatch: the download is not the published file")
    h.controller.check_now()
    h.controller.accept()
    dialog = h.dialogs[0]
    assert dialog.title.cget("text") == "Update not installed"
    assert "checksum mismatch" in dialog.message.cget("text")


def test_page_only_offer_has_no_update_button(root, tmp_path):
    h = _harness(root, tmp_path, can_self_install=False)
    h.controller.check_now()
    assert list(h.dialogs[0].button_widgets) == ["Open release page", "Later"]


def test_check_button_carries_its_help(root, tmp_path):
    from hires.help_widget import help_keys_in

    h = _harness(root, tmp_path)
    widget = add_check_button(root, h.controller)
    assert "hires.about.check_for_updates" in help_keys_in(root)
    widget.invoke()
    assert h.dialogs, "the button did not start a check"
    assert h.dialogs[0].button_widgets.get("Update now") is not None
    assert RELEASE.tag.endswith("13")
