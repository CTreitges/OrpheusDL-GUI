"""Real widget tests for hires.gui_panel.

These build actual tkinter widgets, so they need both tkinter and a display.
Where either is missing the whole module is skipped -- that keeps the headless
CI test job green while still giving real coverage anywhere a display exists
(locally, or under Xvfb).

Run under Xvfb on a headless box:

    xvfb-run -a python -m pytest tests/test_gui_panel.py -v
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

customtkinter = pytest.importorskip("customtkinter", reason="customtkinter not installed")
tkinter = pytest.importorskip("tkinter", reason="tkinter not available")

if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
    pytest.skip("no X display", allow_module_level=True)

from hires.controllers import (  # noqa: E402
    QueueController,
    ReviewRow,
    SpotifyImportController,
    TidalBrowserController,
    UiDispatcher,
)
from hires.gui_panel import (  # noqa: E402
    PlaylistList,
    QueueTab,
    ReviewDialog,
    SpotifyConvertTab,
    TidalPlaylistTab,
    build_tabs,
)
from hires.models import (  # noqa: E402
    ConversionReport,
    MatchCandidate,
    MatchDecision,
    MatchResult,
    PlaylistRef,
    QueueItem,
    TrackRef,
)
from hires.queue_store import QueueStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def root():
    """A real CTk window, destroyed afterwards."""
    try:
        window = customtkinter.CTk()
    except tkinter.TclError as exc:  # pragma: no cover - display trouble
        pytest.skip(f"cannot open a window: {exc}")
    window.geometry("1100x700")
    yield window
    try:
        window.destroy()
    except tkinter.TclError:
        pass


@pytest.fixture
def tabview(root):
    view = customtkinter.CTkTabview(master=root)
    view.pack(fill="both", expand=True)
    return view


@pytest.fixture
def queue(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


def pump(widget, times=3):
    """Let tkinter process pending events, including `after(0, ...)` work."""
    for _ in range(times):
        widget.update_idletasks()
        widget.update()


def pump_until(widget, predicate, timeout=5.0):
    """Pump the event loop until ``predicate()`` holds or the timeout expires.

    Needed wherever a controller does its work on a worker thread: spinning
    ``update()`` without yielding races the thread and would flake.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump(widget)
        if predicate():
            return True
        time.sleep(0.02)
    pump(widget)
    return predicate()


def td_track(tid, title="Song", artist="Artist"):
    return TrackRef(
        id=tid,
        title=title,
        artists=[artist],
        url=f"https://tidal.com/browse/track/{tid}",
        platform="TIDAL",
    )


class FakeTidalLibrary:
    def __init__(self, playlists=None, tracks=None, error=None):
        self._playlists = playlists or []
        self._tracks = tracks or []
        self._error = error

    def list_playlists(self, include_favorites=True):
        if self._error:
            raise self._error
        return list(self._playlists)

    def get_playlist_tracks(self, playlist_id):
        return list(self._tracks)


class FakeSpotifySource:
    def __init__(self, playlists=None, tracks=None):
        self._playlists = playlists or []
        self._tracks = tracks or []

    def list_user_playlists(self):
        return list(self._playlists)

    def get_playlist(self, ref):
        return ref if isinstance(ref, PlaylistRef) else PlaylistRef(id="x", name="Remote")

    def get_playlist_tracks(self, ref):
        return list(self._tracks)


class FakeMatcher:
    def __init__(self, results=None):
        self._results = results or []

    def match_many(self, sources, progress_callback=None):
        if progress_callback:
            for i, r in enumerate(self._results, 1):
                progress_callback(i, len(self._results), r)
        return list(self._results)


# ---------------------------------------------------------------------------
# Queue tab
# ---------------------------------------------------------------------------

class TestQueueTab:
    def test_builds_with_an_empty_queue(self, root, queue):
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        assert tab.frame.winfo_exists()
        assert "empty" in tab.summary_label.cget("text").lower()

    def test_renders_a_row_per_item(self, root, queue):
        queue.add_many(
            [
                QueueItem(url="u1", title="First", subtitle="Artist A", quality="hifi"),
                QueueItem(url="u2", title="Second", subtitle="Artist B", quality="hifi"),
            ]
        )
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        assert len(tab.list_frame.winfo_children()) == 2

    def test_shows_failed_state_and_error(self, root, queue):
        queue.add(QueueItem(url="u1", title="Broken"))
        queue.mark_failed(queue.list()[0].id, "network unreachable")
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        rows = tab.controller.rows()
        assert rows[0].symbol == "✗"
        assert rows[0].error == "network unreachable"

    def test_pause_button_toggles_label_and_state(self, root, queue):
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        tab._toggle_pause()
        pump(root)
        assert queue.is_paused is True
        assert "Resume" in tab.pause_button.cget("text")

        tab._toggle_pause()
        pump(root)
        assert queue.is_paused is False
        assert "Pause" in tab.pause_button.cget("text")

    def test_selecting_then_moving_reorders_the_list(self, root, queue):
        queue.add_many(
            [QueueItem(url="u1", title="First"), QueueItem(url="u2", title="Second")]
        )
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        second = queue.list()[1].id
        tab._select(second)
        pump(root)
        tab._move_up()
        pump(root)

        assert [i.title for i in queue.list()] == ["Second", "First"]

    def test_remove_button_drops_the_selected_item(self, root, queue):
        queue.add(QueueItem(url="u1", title="Doomed"))
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        tab._select(queue.list()[0].id)
        tab._remove()
        pump(root)

        assert queue.list() == []
        assert tab.selected_id is None

    def test_repaints_when_the_queue_changes_from_elsewhere(self, root, queue):
        """A worker thread adding an item must refresh the list."""
        tab = QueueTab(root, QueueController(queue))
        pump(root)
        assert len(tab.controller.rows()) == 0

        queue.add(QueueItem(url="u1", title="Added later"))
        pump(root, times=5)  # subscription hops through after(0, ...)

        assert len(tab.list_frame.winfo_children()) == 1

    def test_retry_failed_button(self, root, queue):
        queue.add(QueueItem(url="u1", title="X"))
        queue.mark_failed(queue.list()[0].id, "boom")
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        tab._retry_failed()
        pump(root)

        assert queue.list()[0].status == "pending"

    def test_clear_finished_button(self, root, queue):
        queue.add_many([QueueItem(url="u1"), QueueItem(url="u2")])
        queue.mark_done(queue.list()[0].id)
        tab = QueueTab(root, QueueController(queue))
        pump(root)

        tab._clear_finished()
        pump(root)

        assert len(queue.list()) == 1

    def test_destroy_unsubscribes(self, root, queue):
        tab = QueueTab(root, QueueController(queue))
        pump(root)
        tab.destroy()

        queue.add(QueueItem(url="u1"))  # must not raise into a dead widget
        pump(root)


# ---------------------------------------------------------------------------
# Playlist list widget
# ---------------------------------------------------------------------------

class TestPlaylistList:
    def test_renders_playlists_with_metadata(self, root):
        selected = []
        widget = PlaylistList(root, selected.append)
        widget.pack(fill="both", expand=True)
        widget.set_playlists(
            [
                PlaylistRef(id="p1", name="Road Trip", track_count=42, owner="Me"),
                PlaylistRef(id="p2", name="Chill", track_count=7),
            ]
        )
        pump(root)

        assert len(widget.frame.winfo_children()) == 2

    def test_empty_list_shows_a_message(self, root):
        widget = PlaylistList(root, lambda p: None)
        widget.pack(fill="both", expand=True)
        widget.set_playlists([])
        pump(root)

        assert len(widget.frame.winfo_children()) == 1

    def test_selecting_reports_the_playlist(self, root):
        picked = []
        widget = PlaylistList(root, picked.append)
        widget.pack(fill="both", expand=True)
        playlist = PlaylistRef(id="p1", name="Road Trip")
        widget.set_playlists([playlist])
        pump(root)

        row = widget.frame.winfo_children()[0]
        widget._select(playlist, row)
        pump(root)

        assert picked == [playlist]
        assert widget.selected is playlist

    def test_set_message_replaces_content(self, root):
        widget = PlaylistList(root, lambda p: None)
        widget.pack(fill="both", expand=True)
        widget.set_playlists([PlaylistRef(id="p1", name="A")])
        pump(root)
        widget.set_message("Nothing here")
        pump(root)

        assert len(widget.frame.winfo_children()) == 1


# ---------------------------------------------------------------------------
# TIDAL tab
# ---------------------------------------------------------------------------

class TestTidalPlaylistTab:
    def _tab(self, root, queue, library, **kwargs):
        controller = TidalBrowserController(
            lambda: library, queue, dispatch=UiDispatcher(root), **kwargs
        )
        return TidalPlaylistTab(root, controller)

    def test_builds_with_an_instruction_message(self, root, queue):
        tab = self._tab(root, queue, FakeTidalLibrary())
        pump(root)

        assert tab.frame.winfo_exists()
        assert tab.download_button.cget("state") == "disabled"

    def test_load_populates_the_list(self, root, queue):
        library = FakeTidalLibrary(
            playlists=[
                PlaylistRef(id="p1", name="Mine", kind="user", track_count=10),
                PlaylistRef(id="p2", name="Saved", kind="favorite"),
            ]
        )
        tab = self._tab(root, queue, library)

        tab.load()
        pump_until(root, lambda: len(tab.list.playlists) == 2)

        assert len(tab.list.playlists) == 2
        assert "2 playlists" in tab.status_label.cget("text")

    def test_error_is_shown_not_raised(self, root, queue):
        tab = self._tab(root, queue, FakeTidalLibrary(error=RuntimeError("login required")))

        tab.load()
        pump_until(root, lambda: "Failed" in tab.status_label.cget("text"))

        assert tab.status_label.cget("text") == "Failed"

    def test_selecting_enables_the_queue_button(self, root, queue):
        playlist = PlaylistRef(id="p1", name="Mine")
        tab = self._tab(root, queue, FakeTidalLibrary(playlists=[playlist]))
        pump(root)

        tab._on_select(playlist)
        pump(root)

        assert tab.download_button.cget("state") == "normal"

    def test_enqueue_puts_the_playlist_in_the_queue(self, root, queue, tmp_path):
        playlist = PlaylistRef(id="p1", name="Mine", platform="TIDAL")
        tab = self._tab(
            root, queue, FakeTidalLibrary(playlists=[playlist]),
            output_provider=lambda: str(tmp_path),
        )
        tab.list.set_playlists([playlist])
        tab.list.selected = playlist
        pump(root)

        tab.enqueue_selected()
        pump(root)

        assert len(queue.list()) == 1
        assert queue.list()[0].url.endswith("/playlist/p1")
        assert queue.list()[0].quality == "hifi"

    def test_enqueue_without_selection_is_harmless(self, root, queue):
        tab = self._tab(root, queue, FakeTidalLibrary())
        pump(root)

        tab.enqueue_selected()  # must not raise
        pump(root)

        assert queue.list() == []


# ---------------------------------------------------------------------------
# Spotify tab
# ---------------------------------------------------------------------------

def accepted(sid, tid):
    return MatchResult(
        source=TrackRef(id=sid, title="Song", artists=["Artist"]),
        best=MatchCandidate(track=td_track(tid), score=0.99, method="isrc"),
        decision=MatchDecision.AUTO_ACCEPT.value,
    )


def review(sid, tid, alternatives=None):
    return MatchResult(
        source=TrackRef(id=sid, title="Song", artists=["Artist"]),
        best=MatchCandidate(
            track=td_track(tid), score=0.71, method="fuzzy", reasons=["title differs"]
        ),
        alternatives=alternatives or [],
        decision=MatchDecision.NEEDS_REVIEW.value,
    )


class TestSpotifyConvertTab:
    def _tab(self, root, queue, source, matcher, **kwargs):
        controller = SpotifyImportController(
            lambda: source, lambda: matcher, queue, dispatch=UiDispatcher(root), **kwargs
        )
        return SpotifyConvertTab(root, controller)

    def test_builds(self, root, queue):
        tab = self._tab(root, queue, FakeSpotifySource(), FakeMatcher())
        pump(root)

        assert tab.frame.winfo_exists()
        assert tab.queue_button.cget("state") == "disabled"
        assert tab.review_button.cget("state") == "disabled"

    def test_empty_url_warns_instead_of_converting(self, root, queue):
        tab = self._tab(root, queue, FakeSpotifySource(), FakeMatcher())
        pump(root)

        tab.convert_from_url()
        pump(root)

        assert "Paste a Spotify playlist link" in tab.status_label.cget("text")

    def test_conversion_updates_progress_and_enables_buttons(self, root, queue):
        source = FakeSpotifySource(tracks=[TrackRef(id="s1", title="Song", artists=["A"])])
        matcher = FakeMatcher([accepted("s1", "t1"), review("s2", "t2")])
        tab = self._tab(root, queue, source, matcher)

        tab.start_conversion(PlaylistRef(id="p1", name="Mix"))
        pump_until(root, lambda: tab.queue_button.cget("state") == "normal")

        assert tab.queue_button.cget("state") == "normal"
        assert tab.review_button.cget("state") == "normal"  # one needs review
        assert "1 matched" in tab.status_label.cget("text")
        assert tab.progress.get() == 1

    def test_queue_matches_only_queues_confident_ones(self, root, queue):
        source = FakeSpotifySource(tracks=[TrackRef(id="s1", title="Song", artists=["A"])])
        matcher = FakeMatcher([accepted("s1", "t1"), review("s2", "t2")])
        tab = self._tab(root, queue, source, matcher)

        tab.start_conversion(PlaylistRef(id="p1", name="Mix"))
        pump_until(root, lambda: tab.report is not None)
        tab.queue_confident()
        pump(root)

        assert len(queue.list()) == 1
        assert "Queued 1 tracks" in tab.status_label.cget("text")

    def test_review_button_stays_disabled_without_uncertain_matches(self, root, queue):
        source = FakeSpotifySource(tracks=[TrackRef(id="s1", title="Song", artists=["A"])])
        tab = self._tab(root, queue, source, FakeMatcher([accepted("s1", "t1")]))

        tab.start_conversion(PlaylistRef(id="p1", name="Mix"))
        pump_until(root, lambda: tab.report is not None)

        assert tab.review_button.cget("state") == "disabled"

    def test_conversion_error_is_displayed(self, root, queue):
        class Boom:
            def get_playlist(self, ref):
                raise RuntimeError("playlist is private")

            def get_playlist_tracks(self, ref):
                raise RuntimeError("playlist is private")

        tab = self._tab(root, queue, Boom(), FakeMatcher())
        tab.start_conversion("https://open.spotify.com/playlist/abc")
        pump_until(root, lambda: "private" in tab.status_label.cget("text"))

        assert "playlist is private" in tab.status_label.cget("text")
        assert queue.list() == []


# ---------------------------------------------------------------------------
# Review dialog
# ---------------------------------------------------------------------------

class TestReviewDialog:
    def _rows(self):
        return [
            ReviewRow(
                source_id="s1",
                source_label="Artist - Song",
                match_label="Artist - Song (Remaster)",
                score=0.71,
                reasons="version tag mismatch",
                alternatives=[("alt1", "Artist - Song", 0.68)],
            ),
            ReviewRow(
                source_id="s2",
                source_label="Other - Track",
                match_label="Other - Track",
                score=0.64,
                reasons="",
                alternatives=[],
            ),
        ]

    def test_builds_one_card_per_row(self, root):
        applied = []
        dialog = ReviewDialog(root, self._rows(), applied.append)
        pump(root)

        assert dialog.window.winfo_exists()
        assert set(dialog.choices) == {"s1", "s2"}
        assert all(v is True for v in dialog.choices.values())  # default: accept
        dialog.window.destroy()

    def test_skip_all_then_apply(self, root):
        applied = []
        dialog = ReviewDialog(root, self._rows(), applied.append)
        pump(root)

        dialog._skip_all()
        dialog._apply()
        pump(root)

        assert applied == [{"s1": False, "s2": False}]

    def test_accept_all_then_apply(self, root):
        applied = []
        dialog = ReviewDialog(root, self._rows(), applied.append)
        pump(root)

        dialog._accept_all()
        dialog._apply()
        pump(root)

        assert applied == [{"s1": True, "s2": True}]

    def test_choosing_an_alternative_reports_its_id(self, root):
        applied = []
        rows = self._rows()
        dialog = ReviewDialog(root, rows, applied.append)
        pump(root)

        var, mapping = dialog._option_vars["s1"]
        alt_option = next(k for k, v in mapping.items() if v == "alt1")
        var.set(alt_option)
        dialog._on_choice("s1")
        dialog._apply()
        pump(root)

        assert applied[0]["s1"] == "alt1"

    def test_cancel_applies_nothing(self, root):
        applied = []
        dialog = ReviewDialog(root, self._rows(), applied.append)
        pump(root)

        dialog.window.destroy()
        pump(root)

        assert applied == []


# ---------------------------------------------------------------------------
# Full wiring
# ---------------------------------------------------------------------------

class TestBuildTabs:
    def test_adds_all_three_tabs(self, root, tabview, queue):
        tabs = build_tabs(
            tabview,
            queue=queue,
            app=root,
            tidal_library_provider=lambda: FakeTidalLibrary(),
            spotify_source_provider=lambda: FakeSpotifySource(),
            matcher_provider=lambda: FakeMatcher(),
        )
        pump(root)

        assert set(tabs) == {"queue", "tidal", "spotify"}
        names = tabview._name_list if hasattr(tabview, "_name_list") else []
        for expected in ("Queue", "TIDAL Playlists", "Spotify → TIDAL"):
            assert expected in names

    def test_tabs_can_be_switched(self, root, tabview, queue):
        build_tabs(
            tabview,
            queue=queue,
            app=root,
            tidal_library_provider=lambda: FakeTidalLibrary(),
            spotify_source_provider=lambda: FakeSpotifySource(),
            matcher_provider=lambda: FakeMatcher(),
        )
        pump(root)

        for name in ("TIDAL Playlists", "Spotify → TIDAL", "Queue"):
            tabview.set(name)
            pump(root)
            assert tabview.get() == name

    def test_queue_tab_reflects_items_added_by_another_tab(self, root, tabview, queue, tmp_path):
        """The real cross-tab path: queue from TIDAL, see it in Queue."""
        playlist = PlaylistRef(id="p1", name="Mine", platform="TIDAL")
        tabs = build_tabs(
            tabview,
            queue=queue,
            app=root,
            tidal_library_provider=lambda: FakeTidalLibrary(playlists=[playlist]),
            spotify_source_provider=lambda: FakeSpotifySource(),
            matcher_provider=lambda: FakeMatcher(),
            output_provider=lambda: str(tmp_path),
        )
        pump(root)

        tabs["tidal"].list.set_playlists([playlist])
        tabs["tidal"].list.selected = playlist
        tabs["tidal"].enqueue_selected()
        pump(root, times=5)

        assert len(tabs["queue"].list_frame.winfo_children()) == 1


# ---------------------------------------------------------------------------
# The real assembly
#
# Every other test injects its own providers, which is exactly how the hi-res
# pinning bug survived 424 tests: setup_tabs() -- the one place where the app is
# actually wired together -- was never exercised. These tests use it directly.
# ---------------------------------------------------------------------------

from hires.integration import setup_tabs  # noqa: E402
from test_integration import FakeGui  # noqa: E402


class TestSetupTabsWiring:
    def _build(self, root, tmp_path, global_quality):
        gui = FakeGui(output_path=str(tmp_path))
        gui.current_settings["globals"]["general"]["quality"] = global_quality
        gui.app = root
        gui.application_path = str(tmp_path)
        tabview = customtkinter.CTkTabview(master=root)
        tabview.pack(fill="both", expand=True)
        result = setup_tabs(gui, tabview, queue_path=str(tmp_path / "queue.json"))
        pump(root)
        return gui, result

    @pytest.mark.parametrize("global_quality", ["high", "low", "lossless", "minimum"])
    def test_downloads_are_pinned_to_hi_res_whatever_the_global_setting(
        self, root, tmp_path, global_quality
    ):
        """The suite promises hi-res on screen and in the docs -- it must deliver it.

        Regression: setup_tabs used to pass the GUI's global quality through,
        so a user with "high" selected silently got 320 kbit AAC.
        """
        gui, result = self._build(root, tmp_path, global_quality)
        assert result is not None, "setup_tabs failed to build the tabs"

        tabs, queue = result["tabs"], result["queue"]
        playlist = PlaylistRef(id="p1", name="Mix", platform="TIDAL")
        tabs["tidal"].list.set_playlists([playlist])
        tabs["tidal"].list.selected = playlist
        tabs["tidal"].enqueue_selected()
        pump(root)

        assert queue.list(), "nothing was queued"
        assert queue.list()[0].quality == "hifi"

    def test_dispatched_download_carries_the_hi_res_override(self, root, tmp_path):
        """End of the chain: what actually reaches gui.py must say hifi."""
        gui, result = self._build(root, tmp_path, "high")
        tabs, queue, runtime = result["tabs"], result["queue"], result["runtime"]

        playlist = PlaylistRef(id="p1", name="Mix", platform="TIDAL")
        tabs["tidal"].list.set_playlists([playlist])
        tabs["tidal"].list.selected = playlist
        tabs["tidal"].enqueue_selected()
        pump(root)

        runtime._pump()

        assert gui.calls, "runtime never handed anything to gui.py"
        _url, _path, data = gui.calls[0]
        assert data["extra_kwargs"]["download_quality_override"] == "hifi"

    def test_all_three_tabs_are_registered(self, root, tmp_path):
        _gui, result = self._build(root, tmp_path, "hifi")
        assert set(result["tabs"]) == {"queue", "tidal", "spotify"}
        assert result["runtime"] is not None
