"""Real widgets: the grouped Queue tab, "Clear all" and the Accounts re-checks.

The decisive tests drive the real assembly (``setup_tabs``) and press the real
buttons, because the wiring is where earlier bugs hid.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

customtkinter = pytest.importorskip("customtkinter", reason="customtkinter not installed")
tkinter = pytest.importorskip("tkinter", reason="tkinter not available")

if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
    pytest.skip("no X display", allow_module_level=True)

from hires import queue_panel  # noqa: E402
from hires.controllers import AccountsController, QueueController, UiDispatcher  # noqa: E402
from hires.dialogs import ConfirmDialog  # noqa: E402
from hires.gui_panel import AccountsTab  # noqa: E402
from hires.integration import setup_tabs  # noqa: E402
from hires.models import AccountState, AccountStatus, QueueItem  # noqa: E402
from hires.queue_panel import QueueTab  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402
from test_gui_panel import pump, pump_until, root  # noqa: E402,F401  (fixture)
from test_integration import FakeGui  # noqa: E402


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "queue.json")


@pytest.fixture
def queue(path):
    return QueueStore(path)


def playlist(group, count):
    return [
        QueueItem(url=f"u-{group}{n}", title=f"{group}{n}", group_id=group, group_label=f"Playlist {group}")
        for n in range(1, count + 1)
    ]


class Confirm:
    """Stands in for the modal dialog: records the question, gives the answer."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def __call__(self, title, message, label):
        self.asked.append((title, message, label))
        return self.answer


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

class TestQueueGroupWidgets:
    def test_queue_group_one_header_per_playlist(self, root, queue):
        queue.add_many(playlist("A", 2) + playlist("B", 3) + [QueueItem(url="loose", title="Loose")])
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        names = [h.name.cget("text") for h in tab._headers]
        assert names == ["Playlist A", "Playlist B", "Single tracks"]
        assert tab._headers[1].progress.cget("text") == "0 of 3 done"
        assert tab.visible_row_count() == 6

    def test_queue_group_lines_sit_under_their_header(self, root, queue):
        queue.add_many(playlist("A", 2) + playlist("B", 1))
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        def grid_row(line):
            return int(line.container.grid_info()["row"])

        a, b = tab._headers
        rows_a = [r for r in tab._rows if r.title.cget("text").startswith("A")]
        rows_b = [r for r in tab._rows if r.title.cget("text").startswith("B")]
        assert grid_row(a) < min(map(grid_row, rows_a)) < grid_row(b) < min(map(grid_row, rows_b))

    def test_queue_group_collapse_and_expand(self, root, queue):
        queue.add_many(playlist("A", 2) + playlist("B", 2))
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        tab.toggle_group("id:A")
        pump(root)
        assert tab.visible_row_count() == 2
        assert tab._headers[0].arrow.cget("text") == "▸"

        tab.toggle_group("id:A")
        pump(root)
        assert tab.visible_row_count() == 4

    def test_group_priority_button_moves_the_playlist_up(self, root, queue):
        queue.add_many(playlist("A", 2) + playlist("B", 2))
        running = queue.claim_next()
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        header_b = next(h for h in tab._headers if h.key == "id:B")
        assert header_b.front_button.cget("state") == "normal"
        header_b.front_button.invoke()
        assert pump_until(root, lambda: queue.next_pending().title == "B1")

        assert queue.get(running.id).status == "active", "the running track was disturbed"
        assert pump_until(
            root,
            lambda: next(h for h in tab._headers if h.key == "id:B").front_button.cget("state") == "disabled",
        ), "B is already next, its button should be off"

    def test_queue_group_remove_asks_and_keeps_the_running_track(self, root, queue):
        queue.add_many(playlist("A", 3))
        queue.claim_next()
        confirm = Confirm(True)
        tab = QueueTab(root, QueueController(queue), confirm=confirm)
        pump(root)

        assert tab.remove_group("id:A")
        assert "finishes first" in confirm.asked[0][1]
        assert [i.title for i in queue.list()] == ["A1"]

    def test_queue_group_2000_entries_stay_bounded(self, root, queue):
        for g in range(20):
            queue.add_many(playlist(f"G{g:02d}", 100))
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        assert tab.visible_group_count() == 20, "every playlist gets its header"
        assert tab.visible_row_count() == queue_panel.MAX_RENDERED_ROWS
        assert "1850" in tab.overflow_label.cget("text")


# ---------------------------------------------------------------------------
# Clear all
# ---------------------------------------------------------------------------

class TestClearAllButton:
    def test_clear_all_no_keeps_everything(self, root, queue):
        queue.add_many(playlist("A", 3))
        confirm = Confirm(False)
        tab = QueueTab(root, QueueController(queue), confirm=confirm)
        pump(root)

        tab.clear_all_button.invoke()
        pump(root)

        assert len(confirm.asked) == 1, "Clear all must ask first"
        assert "3 entries" in confirm.asked[0][1]
        assert len(queue.list()) == 3

    def test_clear_all_disabled_when_empty(self, root, queue):
        tab = QueueTab(root, QueueController(queue), confirm=Confirm(True))
        pump(root)
        assert tab.clear_all_button.cget("state") == "disabled"

    def test_clear_all_through_setup_tabs(self, root, tmp_path, path, monkeypatch):
        """The real button in the real assembly: asks, stops, empties, persists."""
        asked = []

        def fake_dialog(parent, title, message, label):
            asked.append(message)
            return True

        monkeypatch.setattr(queue_panel, "confirm_destructive", fake_dialog)

        gui = FakeGui(output_path=None)
        stops = []
        gui.stop_download = lambda: (stops.append(1), gui.stop_event.set())
        gui.app = root
        gui.application_path = str(tmp_path)
        view = customtkinter.CTkTabview(master=root)
        view.pack(fill="both", expand=True)
        result = setup_tabs(gui, view, queue_path=path)
        pump(root)

        store, runtime, tab = result["queue"], result["runtime"], result["tabs"]["queue"]
        store.add_many(playlist("A", 2) + playlist("B", 2))
        runtime._pump()  # A1 is now downloading
        assert pump_until(root, lambda: tab.visible_group_count() == 2)

        tab.clear_all_button.invoke()
        pump(root)

        assert len(asked) == 1 and "4 entries" in asked[0] and "will be stopped" in asked[0]
        assert stops == [1], "the running download was not stopped"
        assert store.list() == []
        assert QueueStore(path).list() == [], "the cleared queue came back from disk"
        assert pump_until(root, lambda: tab.visible_group_count() == 0)


class TestConfirmDialog:
    def test_clear_all_dialog_cancel_is_the_default(self, root):
        dialog = ConfirmDialog(root, "Clear?", "Remove all?", "Clear all")
        targets = (dialog.cancel_button, *dialog.cancel_button.winfo_children())
        assert pump_until(root, lambda: root.focus_get() in targets), "focus is not on Cancel"
        dialog.window.event_generate("<Escape>")
        pump(root)
        assert dialog.result is False

    def test_clear_all_dialog_confirm_button(self, root):
        dialog = ConfirmDialog(root, "Clear?", "Remove all?", "Clear all")
        pump(root)
        assert dialog.confirm_button.cget("text") == "Clear all"
        dialog.confirm_button.invoke()
        assert dialog.result is True


# ---------------------------------------------------------------------------
# The shared help tooltip
# ---------------------------------------------------------------------------

class TestHelpCoverageTooltip:
    def test_help_coverage_tooltip_appears_on_hover_and_hides_on_leave(self, root):
        from hires import help_widget

        target = customtkinter.CTkButton(root, text="Clear all…")
        target.pack()
        help_widget.attach_tooltip(target, "hires.queue.clear_all")
        pump(root)
        tip = help_widget._TIPS[str(root)]

        tip.schedule(target, target._hires_help_text)
        assert pump_until(root, lambda: tip.window is not None and tip.window.winfo_viewable())
        assert "asking" in tip.label.cget("text") and "Recommended:" in tip.label.cget("text")

        tip.cancel()
        pump(root)
        assert not tip.window.winfo_viewable()

    def test_help_coverage_one_tooltip_window_for_many_markers(self, root):
        from hires import help_widget

        for key in ("hires.queue.pause", "hires.queue.remove", "hires.queue.clear_all"):
            help_widget.attach_tooltip(customtkinter.CTkButton(root, text=key), key)
        pump(root)
        windows = [w for w in root.winfo_children() if isinstance(w, tkinter.Toplevel)]
        assert len(windows) <= 1, f"{len(windows)} tooltip windows for three markers"


# ---------------------------------------------------------------------------
# Accounts: automatic re-checks on screen
# ---------------------------------------------------------------------------

class TestAccountsAutoRefreshOnScreen:
    def test_auto_refresh_tab_counts_and_stops(self, root):
        shown = []

        controller = AccountsController(
            tidal_status_provider=lambda: AccountStatus(service="TIDAL", state=AccountState.SIGNED_OUT),
            spotify_status_provider=lambda: AccountStatus(service="Spotify", state=AccountState.SIGNED_IN),
            dispatch=UiDispatcher(root),
            auto_wait=lambda event, seconds: event.is_set(),
        )
        tab = AccountsTab(root, controller)
        original = tab._show_auto_note
        tab._show_auto_note = lambda text: (shown.append(text), original(text))[1]

        assert pump_until(root, lambda: "Stopped after 5" in tab.summary_label.cget("text"))
        assert [t for t in shown if t.startswith("Checking")] == [f"Checking… ({n}/5)" for n in range(1, 6)]
        assert tab.refresh_button.cget("state") == "normal"

    def test_auto_refresh_soundcloud_and_youtube_cards_explain_and_open_settings(self, root):
        opened = []
        controller = AccountsController(
            tidal_status_provider=lambda: AccountStatus(service="TIDAL", state=AccountState.SIGNED_IN),
            spotify_status_provider=lambda: AccountStatus(service="Spotify", state=AccountState.SIGNED_IN),
            soundcloud_status_provider=lambda: AccountStatus(service="SoundCloud", state=AccountState.SIGNED_IN),
            youtube_status_provider=lambda: AccountStatus(service="YouTube", state=AccountState.SIGNED_OUT),
            dispatch=UiDispatcher(root),
            auto_wait=lambda event, seconds: True,  # stop after the first read
        )
        tab = AccountsTab(root, controller, open_settings=lambda s: opened.append(s) or True)
        pump_until(root, lambda: not controller.is_reading)

        assert list(tab._cards) == ["TIDAL", "Spotify", "SoundCloud", "YouTube"]
        assert "Go+" in tab._cards["SoundCloud"]["info"].cget("text")
        assert tab._cards["YouTube"]["button"].cget("text") == "Open settings"
        tab._cards["YouTube"]["button"].invoke()
        assert opened == ["YouTube"]
