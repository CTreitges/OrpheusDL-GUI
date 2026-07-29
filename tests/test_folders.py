"""Playlist folders: the model, TIDAL's folder view, and batch queueing.

The most important thing tested here is what happens when the folder view is
*not* available. TIDAL exposes folders only on the web player's private v2 API,
which may reject the module's client token or change shape at any time, and
Spotify's Web API has no folder concept at all. Degrading to a flat list is
therefore the normal case, not an error path.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires import tidal_library as tl  # noqa: E402
from hires.controllers import TidalBrowserController, UiDispatcher  # noqa: E402
from hires.models import FolderRef, PlaylistRef, flat_root  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402


@pytest.fixture
def queue(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


def playlist(pid, name=None, tracks=0):
    return PlaylistRef(
        id=pid,
        name=name or f"Playlist {pid}",
        track_count=tracks,
        url=f"https://tidal.com/browse/playlist/{pid}",
        platform="TIDAL",
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class TestFolderRef:
    def test_all_playlists_descends_into_sub_folders(self):
        root = FolderRef(
            playlists=[playlist("a")],
            folders=[
                FolderRef(
                    name="Sets",
                    playlists=[playlist("b")],
                    folders=[FolderRef(name="2026", playlists=[playlist("c")])],
                )
            ],
        )
        assert [p.id for p in root.all_playlists()] == ["a", "b", "c"]
        assert root.total_playlists == 3

    def test_totals_sum_track_counts(self):
        root = FolderRef(
            playlists=[playlist("a", tracks=10)],
            folders=[FolderRef(name="F", playlists=[playlist("b", tracks=5)])],
        )
        assert root.total_tracks == 15

    def test_a_missing_track_count_does_not_break_the_total(self):
        root = FolderRef(playlists=[PlaylistRef(id="x"), playlist("b", tracks=3)])
        assert root.total_tracks == 3

    def test_an_unnamed_root_is_marked_as_such(self):
        assert flat_root([playlist("a")]).is_root
        assert not FolderRef(name="Sets").is_root

    def test_walk_reports_depth(self):
        root = FolderRef(
            folders=[FolderRef(name="A", folders=[FolderRef(name="B")])]
        )
        assert [(f.name, d) for f, d in root.walk()] == [("", 0), ("A", 1), ("B", 2)]

    def test_round_trips_through_a_dict(self):
        root = FolderRef(
            name="Sets",
            playlists=[playlist("a", "Mine", tracks=2)],
            folders=[FolderRef(name="Inner", playlists=[playlist("b")])],
        )
        back = FolderRef.from_dict(root.to_dict())

        assert back.name == "Sets"
        assert [p.id for p in back.all_playlists()] == ["a", "b"]
        assert back.folders[0].name == "Inner"


# ---------------------------------------------------------------------------
# TIDAL folder view
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHttp:
    """Stands in for the module's requests.Session."""

    def __init__(self, pages=None, status_code=200, raises=None):
        #: {folder_id: payload}
        self.pages = pages or {}
        self.status_code = status_code
        self.raises = raises
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.raises:
            raise self.raises
        folder_id = (params or {}).get("folderId")
        payload = self.pages.get(folder_id, {"items": []})
        return FakeResponse(payload, status_code=self.status_code)


class FakeSession:
    def __init__(self, user_id=42, country_code="DE"):
        self.user_id = user_id
        self.country_code = country_code

    def auth_headers(self):
        return {"Authorization": "Bearer token"}


class FolderApi:
    """A TidalApi-alike that also answers the private folder endpoint."""

    def __init__(self, playlists, http, session=None):
        self._playlists = playlists
        self.s = http
        self.session = session if session is not None else FakeSession()

    def authenticated_session(self):
        return self.session

    def iter_user_playlist_entries(self, user_id):
        for p in self._playlists:
            yield {
                "type": "USER_CREATED",
                "playlist": {
                    "uuid": p.id,
                    "title": p.name,
                    "numberOfTracks": p.track_count,
                },
            }


def folder_item(fid, name):
    return {"itemType": "FOLDER", "data": {"id": fid, "name": name}}


def playlist_item(pid):
    return {"itemType": "PLAYLIST", "data": {"uuid": pid}}


class TestTidalFolders:
    def test_playlists_are_grouped_into_their_folders(self):
        http = FakeHttp(
            pages={
                "root": {"items": [folder_item("f1", "Sets")], "totalNumberOfItems": 1},
                "f1": {"items": [playlist_item("a")], "totalNumberOfItems": 1},
            }
        )
        library = tl.TidalLibrary(FolderApi([playlist("a"), playlist("b")], http))

        root = library.list_folders()

        assert [f.name for f in root.folders] == ["Sets"]
        assert [p.id for p in root.folders[0].playlists] == ["a"]
        # 'b' is in no folder, so it stays at the top level.
        assert [p.id for p in root.playlists] == ["b"]

    def test_a_refused_endpoint_degrades_to_a_flat_list(self):
        """401 is the expected answer if TIDAL rejects the TV client token."""
        http = FakeHttp(status_code=401)
        library = tl.TidalLibrary(FolderApi([playlist("a"), playlist("b")], http))

        root = library.list_folders()

        assert root.folders == []
        assert [p.id for p in root.playlists] == ["a", "b"], "playlists must survive"

    def test_a_network_error_degrades_to_a_flat_list(self):
        http = FakeHttp(raises=RuntimeError("connection reset"))
        library = tl.TidalLibrary(FolderApi([playlist("a")], http))

        root = library.list_folders()
        assert [p.id for p in root.all_playlists()] == ["a"]

    def test_an_unrecognisable_payload_degrades_to_a_flat_list(self):
        """The endpoint is undocumented; its shape can change without warning."""
        http = FakeHttp(pages={"root": {"unexpected": "shape"}})
        library = tl.TidalLibrary(FolderApi([playlist("a")], http))

        root = library.list_folders()
        assert [p.id for p in root.all_playlists()] == ["a"]
        assert root.folders == []

    def test_no_playlists_are_lost_when_the_layout_is_partial(self):
        """Half a layout is still better than none -- but nothing may vanish."""
        http = FakeHttp(
            pages={
                "root": {"items": [folder_item("f1", "Sets")]},
                "f1": {"items": [playlist_item("a")]},
            }
        )
        library = tl.TidalLibrary(
            FolderApi([playlist("a"), playlist("b"), playlist("c")], http)
        )

        root = library.list_folders()
        assert sorted(p.id for p in root.all_playlists()) == ["a", "b", "c"]

    def test_a_guest_session_does_not_reach_the_endpoint(self):
        """Without a login there is no collection to read."""
        http = FakeHttp()
        api = FolderApi([playlist("a")], http, session=None)
        api.session = None
        library = tl.TidalLibrary(api)

        with pytest.raises(Exception):
            library.list_folders()
        assert http.calls == [], "must not call the folder endpoint unauthenticated"

    def test_paging_stops_at_the_reported_total(self):
        http = FakeHttp(
            pages={
                "root": {"items": [folder_item("f1", "Sets")], "totalNumberOfItems": 1},
                "f1": {"items": [playlist_item("a")], "totalNumberOfItems": 1},
            }
        )
        tl.TidalLibrary(FolderApi([playlist("a")], http)).list_folders()

        root_calls = [c for c in http.calls if c[1].get("folderId") == "root"]
        assert len(root_calls) == 1, "paged past the end of the collection"

    def test_paging_stops_when_a_page_comes_back_empty(self):
        """A total that never arrives must not become an endless loop."""
        http = FakeHttp(pages={"root": {"items": []}})
        tl.TidalLibrary(FolderApi([playlist("a")], http)).list_folders()
        assert len(http.calls) == 1


# ---------------------------------------------------------------------------
# Batch queueing
# ---------------------------------------------------------------------------

class FlatLibrary:
    def __init__(self, root):
        self.root = root

    def list_folders(self, include_favorites=True):
        return self.root

    def list_playlists(self, include_favorites=True):
        return self.root.all_playlists()


def controller(queue, root, **kwargs):
    return TidalBrowserController(
        lambda: FlatLibrary(root),
        queue,
        dispatch=UiDispatcher(app=None),
        **kwargs,
    )


class TestBatchQueueing:
    def test_enqueue_folder_queues_everything_underneath(self, queue):
        folder = FolderRef(
            name="Sets",
            playlists=[playlist("a", "First"), playlist("b", "Second")],
            folders=[FolderRef(name="Inner", playlists=[playlist("c", "Third")])],
        )
        ctrl = controller(queue, FolderRef(folders=[folder]))

        items = ctrl.enqueue_folder(folder)

        assert len(items) == 3
        assert {i.title for i in items} == {"First", "Second", "Third"}
        # Grouped under the folder, so the queue shows where they came from.
        assert {i.group_label for i in items} == {"Sets"}

    def test_each_playlist_is_its_own_item(self, queue):
        """One failure must not take the rest of the folder with it."""
        folder = FolderRef(name="Sets", playlists=[playlist("a"), playlist("b")])
        controller(queue, FolderRef(folders=[folder])).enqueue_folder(folder)

        assert len(queue.list()) == 2
        assert len({i.id for i in queue.list()}) == 2

    def test_enqueue_many_keeps_going_after_a_bad_playlist(self, queue):
        """A playlist with no id at all should not cost the user the others."""
        ctrl = controller(queue, flat_root([]))
        broken = PlaylistRef(id="", name="Broken")

        items = ctrl.enqueue_many([playlist("a"), broken, playlist("b")])

        assert len(items) >= 2
        assert "a" in {i.source.get("tidal_playlist_id") for i in items}

    def test_a_selection_without_a_folder_has_no_group_label_imposed(self, queue):
        ctrl = controller(queue, flat_root([]))
        items = ctrl.enqueue_many([playlist("a", "Mine")])
        assert items[0].group_label == "Mine"

    def test_hi_res_survives_batch_queueing(self, queue):
        """The whole point of the suite -- it must hold on this path too."""
        folder = FolderRef(name="Sets", playlists=[playlist("a"), playlist("b")])
        ctrl = controller(queue, FolderRef(folders=[folder]), quality_provider=lambda: "hifi")

        items = ctrl.enqueue_folder(folder)
        assert {i.quality for i in items} == {"hifi"}


class TestLoadingFolders:
    def test_the_controller_hands_the_widget_a_folder_tree(self, queue):
        root = FolderRef(folders=[FolderRef(name="Sets", playlists=[playlist("a")])])
        ctrl = controller(queue, root)
        done, errors = [], []

        thread = ctrl.load_playlists(done.append, errors.append)
        thread.join(timeout=5)

        assert errors == []
        assert isinstance(done[0], FolderRef)
        assert [f.name for f in done[0].folders] == ["Sets"]

    def test_a_library_without_folder_support_still_loads(self, queue):
        """Older TidalLibrary objects have no list_folders; they must still work."""

        class OldLibrary:
            def list_playlists(self, include_favorites=True):
                return [playlist("a", "Mine")]

        ctrl = TidalBrowserController(
            lambda: OldLibrary(), queue, dispatch=UiDispatcher(app=None)
        )
        done = []

        ctrl.load_playlists(done.append, lambda _e: None).join(timeout=5)

        assert isinstance(done[0], FolderRef)
        assert [p.name for p in done[0].all_playlists()] == ["Mine"]
