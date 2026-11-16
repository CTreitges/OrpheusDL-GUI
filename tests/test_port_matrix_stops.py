"""Stop, cancel and late-answer paths of the Port and Playlists tabs.

Regression tests for review part 2 (W/review-part2.md, findings 1-10). They
run the real TrackMatcher and count what reaches the (fake) services -- the
earlier tests used fake matchers that passed exceptions on, which is how a
cancel that never stopped anything went unnoticed.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from hires.controllers import UiDispatcher  # noqa: E402
from hires.matcher import TrackMatcher  # noqa: E402
from hires.models import (  # noqa: E402
    HiresError,
    MatchDecision,
    PlaylistRef,
    RateLimitedError,
    RunCancelled,
    TrackRef,
)
from hires.playlist_providers import SERVICES, Pacer, TidalProvider  # noqa: E402
from hires.playlists_controller import PlaylistsController  # noqa: E402
from hires.port_controller import PortController  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402
from hires.soundcloud_source import SoundCloudProvider, track_from_json  # noqa: E402
from hires.tidal_library import TidalLibrary  # noqa: E402
from hires.youtube_source import YouTubeProvider  # noqa: E402
from test_playlist_providers import FakeSoundCloudApi, NoWait, fixture  # noqa: E402,F401
from test_port_matrix import FakeService  # noqa: E402


def controller_with(tmp_path, overrides):
    services = {name: FakeService(name) for name in SERVICES}
    services.update(overrides)
    calls = {name: 0 for name in services}

    def factory(name):
        def make():
            calls[name] += 1
            return services[name]
        return make

    queue = QueueStore(str(tmp_path / "queue.json"))
    ctrl = PortController(
        {name: factory(name) for name in services},
        queue,
        spotify_source_provider=lambda: services["Spotify"],
        dispatch=UiDispatcher(None),
        output_provider=lambda: str(tmp_path),
    )
    return ctrl, queue, services, calls


class CountingTarget(FakeService):
    def __init__(self, service, on_search=None, error_at=None):
        super().__init__(service)
        self.searches = 0
        self.on_search = on_search
        self.error_at = error_at

    def search_tracks(self, query, limit=10):
        self.searches += 1
        if self.on_search:
            self.on_search()
        if self.error_at is not None and self.searches >= self.error_at:
            raise RateLimitedError("HTTP 429")
        return super().search_tracks(query, limit)


# -- 1. cancel / supersede really stops the real matcher -------------------------

def test_port_matrix_cancelled_progress_stops_the_real_matcher():
    searches = []

    class Target:
        def search_tracks(self, query, limit=10):
            searches.append(query)
            return []

    def progress(done, total, result):
        raise RunCancelled("Conversion cancelled")

    with pytest.raises(RunCancelled):
        TrackMatcher(Target()).match_many([TrackRef(id=str(i), title=f"T{i}", artists=["A"]) for i in range(10)], progress)
    assert len(searches) <= 4, "searched on after the run was cancelled"


def test_port_matrix_a_broken_progress_display_still_does_not_stop_matching():
    def progress(done, total, result):
        raise ValueError("widget gone")

    results = TrackMatcher(type("T", (), {"search_tracks": lambda s, q, limit=10: []})()).match_many(
        [TrackRef(id="1", title="A"), TrackRef(id="2", title="B")], progress
    )
    assert len(results) == 2


def test_port_matrix_cancel_mid_port_stops_searching(tmp_path):
    holder = {}
    target = CountingTarget("SoundCloud", on_search=lambda: holder["ctrl"].cancel())
    ctrl, _queue, _services, _calls = controller_with(tmp_path, {"SoundCloud": target})
    holder["ctrl"] = ctrl
    ctrl.set_direction("YouTube", "SoundCloud")
    errors = []
    ctrl.convert("link", lambda *a: None, lambda r: None, errors.append).join(5)
    assert target.searches == 1, f"{target.searches} searches after cancel"


def test_port_matrix_a_superseded_port_stops_searching(tmp_path):
    holder = {}
    target = CountingTarget("SoundCloud", on_search=lambda: holder["ctrl"].set_direction("Spotify", "TIDAL"))
    ctrl, _queue, _services, _calls = controller_with(tmp_path, {"SoundCloud": target})
    holder["ctrl"] = ctrl
    ctrl.set_direction("YouTube", "SoundCloud")
    ctrl.convert("link", lambda *a: None, lambda r: None, lambda m: None).join(5)
    assert target.searches == 1


# -- 2. TIDAL as target: 429 stops, searches are paced -------------------------------

class TidalApi:
    def __init__(self, fail_with):
        self.fail_with = fail_with
        self.calls = 0

    def get_search_data(self, query, limit=20):
        self.calls += 1
        raise Exception(self.fail_with)

    def get_tracks_by_isrc(self, isrc):
        self.calls += 1
        raise Exception(self.fail_with)


@pytest.mark.parametrize("message", ["429 Client Error: Too Many Requests for url: https://x", "Too many requests"])
def test_port_matrix_tidal_rate_limit_is_raised_not_noted(message):
    library = TidalLibrary(TidalApi(message))
    with pytest.raises(RateLimitedError):
        library.search_tracks("x")
    with pytest.raises(RateLimitedError):
        library.find_by_isrc("USQX91300108")


def test_port_matrix_tidal_429_ends_a_port_after_one_request(tmp_path):
    api = TidalApi("429 Client Error: Too Many Requests")
    tidal = TidalProvider(TidalLibrary(api), pacer=NoWait())
    ctrl, queue, _services, _calls = controller_with(tmp_path, {"TIDAL": tidal})
    ctrl.set_direction("YouTube", "TIDAL")
    errors = []
    ctrl.convert("link", lambda *a: None, lambda r: None, errors.append).join(5)
    assert api.calls == 1, f"{api.calls} TIDAL requests after a 429"
    assert errors and ("429" in errors[0] or "limiting" in errors[0])
    assert queue.list() == []


def test_port_matrix_tidal_searches_are_paced_at_one_per_second():
    assert TidalProvider(object()).pacer.interval == 1.0


def test_port_matrix_other_tidal_errors_stay_notes():
    library = TidalLibrary(TidalApi("500 Server Error"))
    assert library.find_by_isrc("USQX91300108") == []
    with pytest.raises(Exception) as caught:
        library.search_tracks("x")
    assert not isinstance(caught.value, RateLimitedError)


# -- 3. SoundCloud Go+ previews are never accepted without asking ---------------------

def test_port_matrix_soundcloud_preview_tracks_go_to_review():
    data = fixture("soundcloud_resolve_playlist")["tracks"][0]  # duration 30 s, full 275 s
    candidate = track_from_json(data)
    assert "30 s preview" in candidate.extra["review_reason"]

    class Target:
        def search_tracks(self, query, limit=10):
            return [candidate]

    source = TrackRef(id="s", title=candidate.title, artists=candidate.artists, duration_sec=275)
    result = TrackMatcher(Target(), target_name="SoundCloud").match(source)
    assert result.best.score >= 0.88
    assert result.decision == MatchDecision.NEEDS_REVIEW.value
    assert any("preview" in r for r in result.best.reasons)


def test_port_matrix_full_soundcloud_tracks_stay_automatic():
    data = dict(fixture("soundcloud_resolve_playlist")["tracks"][0], duration=275435)
    assert "review_reason" not in track_from_json(data).extra


# -- 4. several playlists: a 429 ends the batch ---------------------------------------

def test_port_matrix_batch_stops_at_a_rate_limit(tmp_path):
    target = CountingTarget("SoundCloud", error_at=1)
    source = CountingTarget("YouTube")
    reads = []
    original = source.get_playlist_tracks
    source.get_playlist_tracks = lambda p: reads.append(p) or original(p)
    ctrl, _queue, _services, _calls = controller_with(tmp_path, {"SoundCloud": target, "YouTube": source})
    ctrl.set_direction("YouTube", "SoundCloud")
    playlists = [PlaylistRef(id=str(i), name=f"P{i}") for i in range(3)]
    errors = []
    ctrl.convert_many(playlists, lambda *a: None, lambda *a: None, lambda r: None, errors.append).join(5)
    assert len(reads) == 1, "read the next playlist after the target said stop"
    assert target.searches == 1 and errors


# -- 5. blocks that do not say "429" ----------------------------------------------------

def test_port_matrix_soundcloud_403_on_search_is_a_block():
    api = FakeSoundCloudApi(errors=["This track is not available (e.g. restricted in your country or disabled for API access)."])
    with pytest.raises(RateLimitedError):
        SoundCloudProvider(api, pacer=NoWait()).search_tracks("x")


@pytest.mark.parametrize("message", [
    "This content isn't available, try again later. The current session has been rate-limited by YouTube",
    "HTTP Error 429: Too Many Requests",
])
def test_port_matrix_youtube_rate_limit_wordings_stop(message):
    def extract(url):
        raise RuntimeError(message)

    with pytest.raises(RateLimitedError):
        YouTubeProvider(extract, pacer=NoWait()).search_tracks("x")


# -- 6. Playlists tab: SoundCloud tracks without a login ------------------------------

def test_playlists_tab_soundcloud_tracks_are_read_by_link(tmp_path):
    class ById401(FakeSoundCloudApi):
        def _get(self, path, params=None):
            self.calls.append(("get", path))
            if path.startswith("playlists/"):
                raise Exception("Unauthorized (401). Response: ")
            return super()._get(path, params)

    api = ById401()
    provider = SoundCloudProvider(api, pacer=NoWait(), http_get=lambda u: "")
    controller = PlaylistsController(
        {"SoundCloud": lambda: provider}, QueueStore(str(tmp_path / "q.json")), dispatch=UiDispatcher(None)
    )
    ref = PlaylistRef(id="243061639", name="RAM", url="https://soundcloud.com/daftpunkofficialmusic/sets/ram")
    got, errors = [], []
    controller.load_tracks("SoundCloud", ref, got.append, errors.append).join(5)
    assert errors == [] and got and got[0]


# -- 7. a deleted track does not make the playlist unreadable ------------------------

def test_playlist_provider_soundcloud_skips_tracks_that_are_gone():
    class Gone(FakeSoundCloudApi):
        def resolve_url(self, url):
            data = fixture("soundcloud_resolve_playlist")
            data["tracks"] = [data["tracks"][0], {"id": 1, "kind": "track"}, {"id": 2, "kind": "track"}]
            return data

        def _get(self, path, params=None):
            self.calls.append(("get", path, dict(params or {})))
            if path == "tracks":
                return [{"id": 2, "title": "Still here", "duration": 1000, "user": {"username": "u"}}]
            return super()._get(path, params)

    api = Gone()
    tracks = SoundCloudProvider(api, pacer=NoWait()).get_playlist_tracks("https://soundcloud.com/a/sets/b")
    assert [t.title for t in tracks][1:] == ["Still here"]
    assert ("get", "tracks", {"ids": "1,2"}) in api.calls


# -- 8. a late list for the old direction is dropped ---------------------------------

def test_port_matrix_late_playlists_of_the_old_direction_are_dropped(tmp_path):
    gate = threading.Event()

    class Slow(FakeService):
        def list_folders(self, include_favorites=True):
            gate.wait(5)
            return super().list_folders(include_favorites)

    ctrl, _queue, _services, _calls = controller_with(tmp_path, {"YouTube": Slow("YouTube")})
    ctrl.set_direction("YouTube", "TIDAL")
    roots, errors = [], []
    thread = ctrl.load_playlists(roots.append, errors.append)
    ctrl.set_direction("SoundCloud", "TIDAL")
    gate.set()
    thread.join(5)
    assert roots == [] and errors == []
    assert ctrl.playlists == []


# -- 9. queueing does not build providers on the UI thread ----------------------------

def test_port_matrix_queueing_builds_no_provider(tmp_path):
    ctrl, queue, _services, calls = controller_with(tmp_path, {})
    ctrl.set_direction("YouTube", "TIDAL")
    done = []
    ctrl.convert("link", lambda *a: None, done.append, print).join(5)
    before = dict(calls)
    items = ctrl.enqueue_confident_only(done[0])
    assert calls == before, "queueing loaded a service (may block the window)"
    assert items and {i.quality for i in items} == {"hifi"} and {i.platform for i in items} == {"TIDAL"}
    ctrl.apply_review_and_enqueue({}, report=done[0])
    assert calls == before


# -- N1 (re-check): a TIDAL 403 on search is a block, not a note ------------------

@pytest.mark.parametrize("message", ["403 Client Error: Forbidden for url: https://x", "HTTP 403 Forbidden"])
def test_port_matrix_tidal_403_on_search_stops(message):
    library = TidalLibrary(TidalApi(message))
    with pytest.raises(RateLimitedError):
        library.search_tracks("x")
    with pytest.raises(RateLimitedError):
        library.find_by_isrc("USQX91300108")
