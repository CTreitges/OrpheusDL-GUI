"""The Playlists tab (B2): controller headless, widgets through setup_tabs.

The widget half needs tkinter and a display and skips itself without them;
it drives the real assembly (setup_tabs), not hand-built providers -- the
wiring is where earlier reviews found the bugs.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from hires.controllers import UiDispatcher  # noqa: E402
from hires.models import FolderRef, PlaylistRef, TrackRef, flat_root  # noqa: E402
from hires.playlist_providers import SERVICES, PlaylistProvider, ProviderStatus, UnavailableProvider  # noqa: E402
from hires.playlists_controller import PlaylistsController  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402


class FakeService(PlaylistProvider):
    def __init__(self, service, *, can_list_own=True, hint=""):
        self.service = service
        self._status = ProviderStatus(service, can_list_own=can_list_own, hint=hint)
        self.quality = "hifi" if service == "TIDAL" else None

    def status(self):
        return self._status

    def owns_url(self, url):
        return self.service.lower() in url.lower()

    def list_folders(self, include_favorites=True):
        return flat_root([PlaylistRef(id="p1", name=f"{self.service} Set", platform=self.service,
                                      url=f"https://{self.service.lower()}.example/p1")])

    def get_playlist(self, playlist):
        return PlaylistRef(id="p9", name="Linked", platform=self.service)

    def get_playlist_tracks(self, playlist):
        return [
            TrackRef(id=f"t{i}", title=f"Song {i}", artists=["A"], duration_sec=200,
                     url=f"https://{self.service.lower()}.example/t{i}", platform=self.service)
            for i in range(3)
        ]


def make(tmp_path, services=None):
    services = services or {name: FakeService(name) for name in SERVICES}
    queue = QueueStore(str(tmp_path / "queue.json"))
    controller = PlaylistsController(
        {name: (lambda s=s: s) for name, s in services.items()},
        queue,
        dispatch=UiDispatcher(None),
        output_provider=lambda: str(tmp_path),
    )
    return controller, queue


def run(thread):
    thread.join(5)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class TestPlaylistsTabController:
    def test_offers_the_four_services_in_tab_order(self, tmp_path):
        controller, _queue = make(tmp_path)
        assert controller.services == ["Spotify", "TIDAL", "YouTube", "SoundCloud"]

    def test_status_carries_the_hint_of_a_service_without_login(self, tmp_path):
        services = {name: FakeService(name) for name in SERVICES}
        services["YouTube"] = FakeService("YouTube", can_list_own=False, hint="Export a cookies.txt")
        services["TIDAL"] = UnavailableProvider("TIDAL", "Sign in to TIDAL on the Accounts tab first.")
        controller, _queue = make(tmp_path, services)
        youtube = controller.status("YouTube")
        assert youtube.available and not youtube.can_list_own and "cookies" in youtube.hint
        assert controller.status("TIDAL").available is False

    def test_loading_an_unavailable_service_shows_its_hint(self, tmp_path):
        services = {name: FakeService(name) for name in SERVICES}
        services["TIDAL"] = UnavailableProvider("TIDAL", "Sign in to TIDAL on the Accounts tab first.")
        controller, _queue = make(tmp_path, services)
        errors = []
        run(controller.load_playlists("TIDAL", lambda root: None, errors.append))
        assert errors == ["Sign in to TIDAL on the Accounts tab first."]

    def test_a_whole_youtube_playlist_is_one_item_at_the_gui_quality(self, tmp_path):
        controller, queue = make(tmp_path)
        playlist = PlaylistRef(id="PLx", name="Set", url="https://www.youtube.com/playlist?list=PLx")
        item = controller.enqueue_playlist("YouTube", playlist)
        assert (item.url, item.platform, item.media_kind) == (playlist.url, "YouTube", "playlist")
        assert item.quality is None, "only TIDAL is pinned to hi-res"
        assert item.source == {"kind": "youtube_playlist", "youtube_playlist_id": "PLx"}
        assert len(queue.list()) == 1

    def test_tidal_stays_pinned_to_hi_res(self, tmp_path):
        controller, _queue = make(tmp_path)
        item = controller.enqueue_playlist("TIDAL", PlaylistRef(id="u1", name="Mix"))
        assert item.quality == "hifi" and item.url.endswith("/playlist/u1")

    def test_a_playlist_without_link_is_refused(self, tmp_path):
        controller, queue = make(tmp_path)
        with pytest.raises(Exception, match="no link"):
            controller.enqueue_playlist("SoundCloud", PlaylistRef(id="7", name="Lost"))
        assert queue.list() == []

    def test_picked_tracks_form_one_group_in_their_own_folder(self, tmp_path):
        controller, queue = make(tmp_path)
        playlist = PlaylistRef(id="p1", name="Warm up", platform="SoundCloud")
        tracks = FakeService("SoundCloud").get_playlist_tracks(playlist)[:2]
        items = controller.enqueue_tracks("SoundCloud", playlist, tracks)
        assert len(items) == 2 == len(queue.list())
        assert len({i.group_id for i in items}) == 1 and {i.group_label for i in items} == {"Warm up"}
        assert {i.platform for i in items} == {"SoundCloud"} and {i.quality for i in items} == {None}
        assert {i.output_path for i in items} == {os.path.join(str(tmp_path), "Warm up")}

    def test_playlist_names_cannot_leave_the_download_folder(self, tmp_path):
        """Foreign playlist titles are untrusted (threat T5)."""
        controller, _queue = make(tmp_path)
        playlist = PlaylistRef(id="p1", name="../../Windows/System32", platform="YouTube")
        tracks = FakeService("YouTube").get_playlist_tracks(playlist)[:1]
        item = controller.enqueue_tracks("YouTube", playlist, tracks)[0]
        target = os.path.realpath(item.output_path)
        assert os.path.dirname(target) == os.path.realpath(str(tmp_path))

    def test_open_link_refuses_a_link_of_another_service(self, tmp_path):
        controller, _queue = make(tmp_path)
        errors = []
        run(controller.open_link("YouTube", "https://open.spotify.com/playlist/x", lambda p: None, errors.append))
        assert errors == ["That is not a YouTube link."]

    def test_open_link_resolves_and_keeps_the_link(self, tmp_path):
        controller, _queue = make(tmp_path)
        found = []
        run(controller.open_link("SoundCloud", " https://soundcloud.example/a/sets/b ", found.append, print))
        assert found[0].name == "Linked" and found[0].url == "https://soundcloud.example/a/sets/b"


# ---------------------------------------------------------------------------
# Widgets, through the real assembly
# ---------------------------------------------------------------------------

@pytest.fixture
def window():
    customtkinter = pytest.importorskip("customtkinter")
    tkinter = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
        pytest.skip("no X display")
    root = None
    # The Windows venv's Tcl sometimes fails to initialise ("tk wasn't
    # installed properly") and works on the next try; three tries, then skip.
    for attempt in range(3):
        try:
            root = customtkinter.CTk()
            break
        except tkinter.TclError as exc:  # pragma: no cover
            if attempt == 2:
                pytest.skip(f"cannot open a window: {exc}")
            time.sleep(0.2)
    root.geometry("1100x700")
    yield root
    try:
        root.destroy()
    except tkinter.TclError:
        pass


def pump_until(root, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update_idletasks()
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def assemble(window, tmp_path):
    import customtkinter

    from hires.integration import setup_tabs
    from test_integration import FakeGui

    gui = FakeGui(output_path=str(tmp_path))
    gui.app = window
    gui.application_path = str(tmp_path)
    view = customtkinter.CTkTabview(master=window)
    view.pack(fill="both", expand=True)
    result = setup_tabs(gui, view, queue_path=str(tmp_path / "queue.json"))
    assert result is not None, "setup_tabs failed"
    return view, result


class TestPlaylistsTabWidgets:
    def test_playlists_tab_replaces_the_tidal_tab(self, window, tmp_path):
        view, result = assemble(window, tmp_path)
        names = list(getattr(view, "_name_list", []))
        assert "Playlists" in names and "Port" in names
        assert "TIDAL Playlists" not in names and "Spotify → TIDAL" not in names
        assert result["tabs"]["playlists"].controller.services == list(SERVICES)

    def test_playlists_tab_explains_a_service_it_cannot_reach(self, window, tmp_path):
        """N3: FakeGui has no TIDAL module and no YouTube cookie file."""
        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["playlists"]
        assert tab.service == "TIDAL"
        assert pump_until(window, lambda: "TIDAL" in tab.hint_label.cget("text"))
        assert tab.load_button.cget("state") == "normal"  # stays usable; the hint explains

        tab.set_service("YouTube")
        assert pump_until(window, lambda: tab.hint_label.cget("text") != "")
        assert tab.load_button.cget("state") == "normal"  # stays usable; the hint explains
        assert "YouTube" in tab.list._message.cget("text") if hasattr(tab.list, "_message") else True

    def test_playlists_tab_queues_picked_tracks_as_one_group(self, window, tmp_path):
        _view, result = assemble(window, tmp_path)
        tab, queue = result["tabs"]["playlists"], result["queue"]
        fake = FakeService("YouTube")
        tab.controller.providers["YouTube"] = lambda: fake
        tab.controller.browsers["YouTube"].library_provider = lambda: fake

        tab.set_service("YouTube")
        tab.url_entry.insert(0, "https://youtube.example/playlist?list=PL1")
        tab.open_link()
        assert pump_until(window, lambda: len(tab.tracks) == 3), "tracks never arrived"
        assert tab.track_list.size() == 3

        tab.track_list.selection_set(0, 1)
        tab._on_track_pick()
        assert "Queue 2 picked" in tab.tracks_button.cget("text")
        tab.enqueue_picked_tracks()

        items = queue.list()
        assert [i.title for i in items] == ["Song 0", "Song 1"]
        assert {i.group_label for i in items} == {"Linked"} and {i.platform for i in items} == {"YouTube"}

    def test_a_late_answer_for_another_service_is_ignored(self, window, tmp_path):
        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["playlists"]
        tab.set_service("SoundCloud")
        tab._on_loaded("YouTube", FolderRef(playlists=[PlaylistRef(id="x", name="Stale")]))
        assert tab.list.playlists == []

    def test_playlists_tab_works_once_a_late_module_has_loaded(self, window, tmp_path):
        """TIDAL often loads after the window: My playlists must not stay dead."""
        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["playlists"]
        assert pump_until(window, lambda: "TIDAL" in tab.hint_label.cget("text"))
        assert tab.load_button.cget("state") == "normal"

        late = FakeService("TIDAL")  # the module has loaded meanwhile
        tab.controller.providers["TIDAL"] = lambda: late
        tab.controller.browsers["TIDAL"].library_provider = lambda: late
        tab.load_button.invoke()
        assert pump_until(window, lambda: len(tab.list.playlists) == 1)
        assert pump_until(window, lambda: tab.hint_label.cget("text") == "")
